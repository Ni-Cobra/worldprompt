"""Core settings lifecycle and browser-to-local-service boundaries."""
import copy
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import threading
import time
from urllib.request import Request, urlopen

import pytest

from worldprompt_core import agent, server
from worldprompt_core.config import load_config, validate_config
from worldprompt_core.world_jobs import JobSupervisor
from test_world_jobs import submission


def wait_for(check, seconds=3):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if check():
            return
        time.sleep(.01)
    assert check(), "Timed out waiting for asynchronous fixture"


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setenv("WORLDPROMPT_CONNECTION_FILE", str(tmp_path / "connection.json"))
    settings = validate_config({"jobs_dir": str(tmp_path / "jobs"),
                                "codex_command": ["codex-fixture"], "claude_command": ["claude-fixture"]})
    supervisor = JobSupervisor(settings, start=False)
    token = tmp_path / "adapter-token.txt"
    token.write_text("fixture-adapter-token-do-not-expose")
    instance = server.CoreApp(settings, supervisor, token, "http://127.0.0.1:8768", tmp_path / "config.json")
    try:
        yield instance
    finally:
        supervisor.close()


def test_restart_settings_are_saved_without_repointing_running_service(app, tmp_path):
    original = copy.deepcopy(app.config)
    changed = dict(original, provider="claude", world_port=8878, jobs_dir=str(tmp_path / "new-jobs"),
                   world_concurrency=3, world_max_pending=20, world_lease_seconds=200,
                   world_max_jobs_per_player=3, job_limits=True)
    # Job limits are off by default, so the stored values are not enforced.
    assert (app.supervisor.concurrency, app.supervisor.max_jobs_per_player) == (1_000_000, 1_000_000)
    result = app.update_config(changed)
    assert result["restart_required"] is True
    assert load_config(app.config_path) == changed
    assert app.supervisor.config["jobs_dir"] == original["jobs_dir"]
    assert app.supervisor.config["world_port"] == original["world_port"]
    assert app.supervisor.config["provider"] == "claude"
    assert (app.supervisor.concurrency, app.supervisor.max_pending, app.supervisor.lease_seconds) == (3, 20, 200)
    assert app.supervisor.max_jobs_per_player == 3
    assert result["service"]["world_url"] == "http://127.0.0.1:8768"
    reverted = dict(changed, world_port=original["world_port"], jobs_dir=original["jobs_dir"])
    assert app.update_config(reverted)["restart_required"] is False
    app.update_config(dict(reverted, job_limits=False))
    assert (app.supervisor.concurrency, app.supervisor.max_pending, app.supervisor.max_jobs_per_player) == (
        1_000_000, 1_000_000, 1_000_000)
    assert load_config(app.config_path)["world_concurrency"] == 3


def test_active_job_prevents_reconfiguration_and_persistence(app):
    queued = app.supervisor.submit(submission())
    original = copy.deepcopy(app.config)
    with pytest.raises(ValueError, match="active Minecraft jobs"):
        app.update_config(dict(original, provider="claude"))
    assert app.config == original
    assert not app.config_path.exists()
    app.supervisor.cancel(queued["id"])
    assert app.update_config(dict(original, provider="claude"))["config"]["provider"] == "claude"


def test_save_failure_keeps_running_configuration(app, monkeypatch):
    original = copy.deepcopy(app.config)
    def fail(*args):
        raise OSError("Fixture persistence failure")
    monkeypatch.setattr(server, "save_config", fail)
    with pytest.raises(OSError):
        app.update_config(dict(original, provider="claude"))
    assert app.config == original
    assert app.supervisor.config == original
    assert not app.restart_required


def test_pairing_preserves_minecraft_options_and_backups_original(app, tmp_path):
    minecraft = tmp_path / "instance"
    (minecraft / "mods").mkdir(parents=True)
    (minecraft / "config").mkdir()
    target = minecraft / "config/worldprompt.json"
    original = {"blocksPerTick": 512, "pollMilliseconds": 750, "maxParticleSegments": 48,
                "serviceUrl": "http://127.0.0.1:9999", "token": "old-inline-token", "futureOption": {"nested": True}}
    target.write_text(json.dumps(original), encoding="utf-8")
    result = app.connect_minecraft(str(minecraft))
    saved = json.loads(target.read_text(encoding="utf-8"))
    assert saved["serviceUrl"] == app.world_url
    assert saved["tokenFile"] == str(app.token_path.resolve())
    assert "token" not in saved
    for field in ("blocksPerTick", "pollMilliseconds", "maxParticleSegments", "futureOption"):
        assert saved[field] == original[field]
    backups = list(target.parent.glob("worldprompt.backup-*.json"))
    assert len(backups) == 1
    assert json.loads(backups[0].read_text(encoding="utf-8")) == original
    assert result["path"] == str(target)
    assert app.token_path.read_text() == "fixture-adapter-token-do-not-expose"


def test_pairing_keeps_only_the_newest_three_config_backups(app, tmp_path):
    minecraft = tmp_path / "instance"
    (minecraft / "mods").mkdir(parents=True)
    (minecraft / "config").mkdir()
    target = minecraft / "config/worldprompt.json"
    target.write_text(json.dumps({"blocksPerTick": 512}), encoding="utf-8")
    old = []
    for age in range(5):  # worldprompt.backup-0 is the oldest
        backup = target.with_name(f"worldprompt.backup-{age}.json")
        backup.write_text("{}", encoding="utf-8")
        os.utime(backup, (1_000_000 + age, 1_000_000 + age))
        old.append(backup)
    unrelated = target.with_name("worldprompt.json.bak")
    unrelated.write_text("keep", encoding="utf-8")
    app.connect_minecraft(str(minecraft))
    remaining = sorted(target.parent.glob("worldprompt.backup-*.json"))
    assert len(remaining) == 3
    assert old[3] in remaining and old[4] in remaining and not any(path in remaining for path in old[:3])
    new = [path for path in remaining if path not in old]
    assert len(new) == 1 and json.loads(new[0].read_text(encoding="utf-8")) == {"blocksPerTick": 512}
    assert unrelated.read_text(encoding="utf-8") == "keep"


@pytest.mark.parametrize("wsl", [True, False])
def test_mnt_paths_are_translated_only_on_wsl(app, monkeypatch, wsl):
    from pathlib import PurePosixPath
    calls = []
    def fake_run(command, **kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(command, 0, "C:\\Users\\me\\token\n", "")
    monkeypatch.setattr(server.wsl.subprocess, "run", fake_run)
    monkeypatch.setattr(server.wsl, "is_wsl", lambda: wsl)
    token = app.adapter_token_file(PurePosixPath("/mnt/c/Users/me/instance"))
    if wsl:
        assert calls[0][:2] == ["wslpath", "-w"]
        assert token == "C:\\Users\\me\\token"
    else:
        assert calls == []  # A real /mnt mount (for example on a VPS) keeps its POSIX path.
        assert token == str(app.token_path.resolve())
    calls.clear()
    app.adapter_token_file(PurePosixPath("/home/me/instance"))
    assert calls == []


def test_malformed_minecraft_config_is_not_overwritten(app, tmp_path):
    minecraft = tmp_path / "instance"
    (minecraft / "mods").mkdir(parents=True)
    (minecraft / "config").mkdir()
    target = minecraft / "config/worldprompt.json"
    target.write_text("not valid JSON")
    with pytest.raises(ValueError):
        app.connect_minecraft(str(minecraft))
    assert target.read_text() == "not valid JSON"
    assert not list(target.parent.glob("*.backup-*.json"))


@pytest.mark.parametrize("folder", ["", "   ", "\t\n", None, [], {}, 123, "bad\x00path"])
def test_invalid_pairing_folder_cannot_target_current_directory(app, tmp_path, monkeypatch, folder):
    (tmp_path / "mods").mkdir()
    monkeypatch.chdir(tmp_path)
    with pytest.raises(ValueError, match="Minecraft instance"):
        app.connect_minecraft(folder)
    assert not (tmp_path / "config/worldprompt.json").exists()


def test_provider_check_does_not_restore_status_from_old_settings(app, monkeypatch):
    started, finish = threading.Event(), threading.Event()
    def fake_check(config):
        started.set()
        assert finish.wait(3)
        return {"ready": True, "message": "Old executable was signed in"}
    monkeypatch.setattr(agent, "check_provider", fake_check)
    app.check_provider("claude")
    assert started.wait(3)
    assert app.snapshot()["providers"]["claude"]["checking"]
    app.update_config(dict(app.config, claude_command=["replacement-cli"]))
    finish.set()
    wait_for(lambda: not any(t.name == "provider-status" and t.is_alive() for t in threading.enumerate()))
    assert "claude" not in app.snapshot()["providers"]


def test_check_unsaved_command_and_automatic_retry_do_not_change_settings(gui, app, monkeypatch):
    from worldprompt_core import models
    calls = []
    def check(config):
        calls.append(config.command)
        return {"ready": not config.command, "message": "Checked", "command": ["/apps/codex"]}
    monkeypatch.setattr(agent, "check_provider", check)
    monkeypatch.setattr(models, "list_models", lambda config: [])
    original = copy.deepcopy(app.config)
    for command, ready in [(["missing-cli"], False), ([], True)]:
        assert gui("POST", "/api/providers/check", {"provider": "codex", "command": command})[0] == 202
        wait_for(lambda: not app.providers['codex']['checking'])
        result = app.providers['codex']
        assert result['ready'] is ready and result['requested_command'] == command
        assert result['check_id']
    assert calls == [('missing-cli',), ()]
    assert app.config == original and not app.config_path.exists()
    assert app.providers['codex']['command'] == ['/apps/codex']


def test_later_command_check_wins_over_slow_earlier_result(app, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    def check(config):
        if config.command == ('old-cli',):
            entered.set()
            assert release.wait(3)
        return {'ready': False, 'message': config.command[0]}
    monkeypatch.setattr(agent, 'check_provider', check)
    app.check_provider('codex', ['old-cli'])
    assert entered.wait(3)
    try:
        app.check_provider('codex', ['new-cli'])
        wait_for(lambda: not app.providers['codex']['checking'])
    finally:
        release.set()
    wait_for(lambda: not any(t.name == 'provider-status' and t.is_alive() for t in threading.enumerate()))
    assert app.providers['codex']['message'] == 'new-cli'


def test_repeated_connection_checks_share_inflight_model_refresh(app, monkeypatch):
    from worldprompt_core import models
    entered, release = threading.Event(), threading.Event()
    calls = []
    def discover(config):
        calls.append(config.command)
        entered.set()
        assert release.wait(3)
        return [{'id': 'model', 'name': 'Model', 'supports_images': True}]
    monkeypatch.setattr(models, 'list_models', discover)
    monkeypatch.setattr(agent, 'check_provider', lambda config: {
        'ready': True, 'message': 'Connected', 'command': ['/apps/codex']})
    app.check_provider('codex', [])
    assert entered.wait(3)
    try:
        app.check_provider('codex', [])
        wait_for(lambda: not app.providers['codex']['checking'])
        assert calls == [('/apps/codex',)]
    finally:
        release.set()
    wait_for(lambda: not app.models['codex']['loading'])


@pytest.mark.parametrize('command', ['codex', [None], [''], ['bad\x00command']])
def test_command_checks_reject_invalid_or_unauthenticated_arguments(gui, app, command):
    body = {'provider': 'codex', 'command': command}
    assert gui('POST', '/api/providers/check', body, {'X-WorldPrompt-Session': 'wrong'})[0] == 403
    assert gui('POST', '/api/providers/check', body)[0] == 400
    assert not app.providers


@pytest.fixture
def gui(app):
    http = ThreadingHTTPServer(("127.0.0.1", 0), server.make_handler(app))
    thread = threading.Thread(target=http.serve_forever, daemon=True)
    thread.start()
    host = f"127.0.0.1:{http.server_port}"
    def request(method, path, body=None, headers=None, raw=False):
        connection = HTTPConnection("127.0.0.1", http.server_port, timeout=3)
        effective = {"Host": host}
        if method == "GET" and path == "/api/state":
            effective["X-WorldPrompt-Session"] = app.session
        if body is not None:
            effective.update({"Content-Type": "application/json", "Origin": "http://" + host,
                              "X-WorldPrompt-Session": app.session})
        effective.update(headers or {})
        encoded = json.dumps(body).encode() if body is not None else None
        try:
            connection.request(method, path, encoded, effective)
            response = connection.getresponse()
            payload = response.read()
            return response.status, dict(response.getheaders()), payload if raw else json.loads(payload)
        finally:
            connection.close()
    try:
        yield request
    finally:
        http.shutdown()
        thread.join(timeout=3)
        http.server_close()


def test_state_is_local_uncacheable_and_does_not_contain_adapter_secret(gui, app):
    status, headers, body = gui("GET", "/api/state")
    assert status == 200
    assert headers["Cache-Control"] == "no-store"
    assert "frame-ancestors 'none'" in headers["Content-Security-Policy"]
    assert "session" not in body
    assert "fixture-adapter-token-do-not-expose" not in json.dumps(body)
    assert gui("GET", "/api/state", headers={"X-WorldPrompt-Session": ""})[0] == 403
    assert gui("GET", "/api/state", headers={"X-WorldPrompt-Session": "wrong"})[0] == 403
    assert gui("GET", "/api/state", headers={"Host": "attacker.example:8765"})[0] == 403


def test_gui_serves_only_listed_static_assets_with_explicit_types(gui):
    page = (server.STATIC_DIR / "index.html").read_text(encoding="utf-8")
    stylesheet = (server.STATIC_DIR / "style.css").read_text(encoding="utf-8")
    for path, (name, content_type) in server.STATIC_FILES.items():
        status, headers, body = gui("GET", path, raw=True)
        assert status == 200, path
        assert headers["Content-Type"] == content_type
        assert headers["X-Content-Type-Options"] == "nosniff"
        assert "default-src 'self'" in headers["Content-Security-Policy"]
        assert body == (server.STATIC_DIR / name).read_bytes()
        # Every served asset is one the page actually references, and vice versa for fonts.
        assert path == "/" or f'"{path}"' in page + stylesheet, path
    assert stylesheet.count('url("/fonts/') == sum(path.startswith("/fonts/") for path in server.STATIC_FILES)
    for path in ("/fonts/LICENSE-newsreader.txt", "/fonts/", "/static/app.js", "/../server.py", "/favicon.ico"):
        assert gui("GET", path, raw=True)[0] == 404, path


def test_model_refresh_is_authenticated_and_keeps_last_list_on_failure(gui, app, monkeypatch):
    from worldprompt_core import models
    items = [{"id": "fixture-model", "name": "Fixture", "supports_images": True}]
    monkeypatch.setattr(models, "list_models", lambda config: items)
    assert gui("POST", "/api/providers/models", {"provider": "codex"},
               {"X-WorldPrompt-Session": "wrong"})[0] == 403
    assert app.models == {}
    assert gui("POST", "/api/providers/models", {"provider": []})[0] == 400
    assert gui("POST", "/api/providers/models", {"provider": "codex"})[0] == 202
    wait_for(lambda: not app.snapshot()["models"]["codex"]["loading"])
    assert app.snapshot()["models"]["codex"]["items"] == items
    def failed(config):
        raise OSError("private executable path")
    monkeypatch.setattr(models, "list_models", failed)
    app.refresh_models("codex")
    wait_for(lambda: not app.snapshot()["models"]["codex"]["loading"])
    result = app.snapshot()["models"]["codex"]
    assert result["items"] == items and result["error"]
    assert "private executable" not in json.dumps(result)


def test_model_discovery_deduplicates_requests_and_discards_stale_results(app, monkeypatch):
    from worldprompt_core import models
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()
    calls = []
    def discover(config):
        calls.append(config.command)
        entered.set()
        release.wait(3)
        finished.set()
        return [{"id": "old-cli-model", "name": "Old", "supports_images": True}]
    monkeypatch.setattr(models, "list_models", discover)
    app.refresh_models("codex")
    assert entered.wait(2)
    try:
        app.refresh_models("codex")
        assert len(calls) == 1
        app.update_config(dict(app.config, codex_model="custom-model"))
        assert app.models["codex"]["loading"]
        app.update_config(dict(app.config, codex_command=["replacement-cli"]))
        assert "codex" not in app.models
    finally:
        release.set()
    assert finished.wait(2)
    wait_for(lambda: not any(t.name == "provider-models" and t.is_alive() for t in threading.enumerate()))
    assert "codex" not in app.models


@pytest.mark.parametrize("headers", [
    {"X-WorldPrompt-Session": ""}, {"X-WorldPrompt-Session": "wrong"},
    {"Origin": "https://attacker.example"}, {"Origin": "null"},
    {"Sec-Fetch-Site": "cross-site"}, {"Host": "attacker.example:8765"},
])
def test_cross_site_or_unauthenticated_settings_mutations_are_rejected(gui, app, headers):
    before = copy.deepcopy(app.config)
    status, _, _ = gui("POST", "/api/config", dict(before, provider="claude"), headers)
    assert status == 403
    assert app.config == before
    assert not app.config_path.exists()


@pytest.mark.parametrize("path", ["/api/providers/login", "/api/providers/login/input", "/api/providers/login/cancel"])
def test_gui_no_longer_exposes_login_endpoints(gui, app, monkeypatch, path):
    def unexpected_launch(*args, **kwargs):
        pytest.fail("Removed login endpoints must not launch a process")
    monkeypatch.setattr(server.subprocess, "Popen", unexpected_launch)
    status, _, result = gui("POST", path, {"provider": "codex", "value": "unused-code"})
    assert status == 404
    assert result["error"] == "Not found."
    assert "login" not in app.snapshot()


def test_gui_saves_supported_provider_and_rejects_api_configuration(gui, app):
    status, _, saved = gui("POST", "/api/config", dict(app.config, provider="claude"))
    assert status == 200
    assert saved["config"]["provider"] == "claude"
    assert load_config(app.config_path)["provider"] == "claude"
    status, _, error = gui("POST", "/api/config", dict(app.config, provider="anthropic"))
    assert status == 400 and "not supported" in error["error"]
    assert load_config(app.config_path)["provider"] == "claude"
    assert gui("POST", "/api/config", dict(app.config, api_key="fixture-key"))[0] == 400
    assert "api_key" not in app.config_path.read_text()


@pytest.mark.parametrize("replacement, malformed_access", [(False, False), (True, False), (False, True)])
def test_service_shutdown_removes_only_its_own_discovery_files(tmp_path, monkeypatch, replacement, malformed_access):
    import socket
    discovery_path = tmp_path / "connection.json"
    monkeypatch.setenv("WORLDPROMPT_CONNECTION_FILE", str(discovery_path))
    monkeypatch.setattr(server.CoreApp, "check_provider", lambda self, provider: None)
    with socket.socket() as port_reservation:
        port_reservation.bind(("127.0.0.1", 0))
        world_port = port_reservation.getsockname()[1]
    settings = validate_config({"jobs_dir": str(tmp_path / "jobs"), "world_port": world_port})
    listeners, errors = [], []
    def listener(*args, **kwargs):
        http = ThreadingHTTPServer(*args, **kwargs)
        listeners.append(http)
        return http
    monkeypatch.setattr(server, "ThreadingHTTPServer", listener)
    def run():
        try:
            server.serve(settings, port=0, config_path=tmp_path / "settings.json", open_browser=False)
        except Exception as exc:
            errors.append(exc)
    thread = threading.Thread(target=run, name="core-service-fixture", daemon=True)
    thread.start()
    try:
        wait_for(lambda: bool(errors) or discovery_path.exists(), seconds=5)
        assert not errors
        assert len(listeners) == 2
        discovery = json.loads(discovery_path.read_text(encoding="utf-8"))
        assert discovery["serviceUrl"] == f"http://127.0.0.1:{world_port}"
        token_file = Path(discovery["tokenFile"])
        assert token_file.is_file()
        connection = HTTPConnection("127.0.0.1", listeners[-1].server_port, timeout=3)
        access_file = tmp_path / "settings.json.gui-access.json"
        wait_for(access_file.exists)
        if os.name == "posix":
            assert access_file.stat().st_mode & 0o777 == 0o600
        session = json.loads(access_file.read_text(encoding="utf-8"))["session"]
        connection.request("GET", "/api/state", headers={"X-WorldPrompt-Session": session})
        response = connection.getresponse()
        state = json.loads(response.read())
        connection.close()
        assert response.status == 200
        other = {"serviceUrl": "http://127.0.0.1:19999", "tokenFile": str(tmp_path / "different-token")}
        if replacement:
            discovery_path.write_text(json.dumps(other), encoding="utf-8")
        if malformed_access:
            access_file.write_text("[]", encoding="utf-8")
        connection = HTTPConnection("127.0.0.1", listeners[-1].server_port, timeout=3)
        connection.request("POST", "/api/shutdown", b"{}", {
            "Content-Type": "application/json", "X-WorldPrompt-Session": session})
        response = connection.getresponse()
        response.read()
        connection.close()
        assert response.status == 200
        thread.join(timeout=5)
        assert not thread.is_alive()
        assert not errors
        if replacement:
            assert json.loads(discovery_path.read_text()) == other
        else:
            assert not discovery_path.exists()
        assert token_file.is_file(), "Persistent adapter token must survive application restarts"
        if malformed_access:
            assert json.loads(access_file.read_text()) == []
        else:
            assert not access_file.exists(), "Ephemeral GUI access must not survive shutdown"
    finally:
        if thread.is_alive() and len(listeners) == 2:
            listeners[-1].shutdown()
            thread.join(timeout=5)


def _start_serving_core(tmp_path, attempt):
    """Launch `serve` on an OS-assigned GUI port; the live addresses come from Core's own files."""
    with socket.socket() as probe:  # world_port cannot be 0 in settings, so probe one and retry on a lost race
        probe.bind(("127.0.0.1", 0))
        world = probe.getsockname()[1]
    settings = tmp_path / f"config-{attempt}.json"
    settings.write_text(json.dumps({"world_port": world, "jobs_dir": str(tmp_path / "jobs"),
                                   "codex_command": ["missing-test-cli"]}))
    connection = tmp_path / "connection.json"
    environment = dict(os.environ, WORLDPROMPT_CONNECTION_FILE=str(connection),
                       PYTHONPATH=str(Path(__file__).resolve().parents[1] / "src"))
    process = subprocess.Popen([sys.executable, "-m", "worldprompt_core", "--config", str(settings),
                                "serve", "--no-browser", "--port", "0"], env=environment,
                               stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    return process, settings, connection


@pytest.mark.skipif(os.name == "nt", reason="POSIX SIGTERM service shutdown")
def test_sigterm_removes_discovery_and_releases_directory(tmp_path):
    process = None
    try:
        for attempt in range(3):
            process, settings, connection = _start_serving_core(tmp_path, attempt)
            deadline = time.monotonic() + 30
            gui = None
            while time.monotonic() < deadline and process.poll() is None:
                access_file = settings.with_name(settings.name + ".gui-access.json")
                try:
                    access = json.loads(access_file.read_text())
                    gui = access["port"]
                    request = Request(f"http://127.0.0.1:{gui}/api/state",
                                      headers={"X-WorldPrompt-Session": access["session"]})
                    with urlopen(request, timeout=2) as response:
                        assert json.load(response)["product"] == "WorldPrompt Core"
                    if connection.exists():
                        break
                except (OSError, ValueError, KeyError):
                    pass
                time.sleep(.05)
            else:
                if process.poll() is not None and attempt < 2:  # lost the world-port race; try new ports
                    process.stderr.close()
                    process = None
                    continue
                raise AssertionError("Core did not start serving")
            break
        discovery = json.loads(connection.read_text())
        assert discovery["serviceUrl"].startswith("http://127.0.0.1:")
        assert Path(discovery["tokenFile"]).is_file()
        process.send_signal(signal.SIGTERM)
        assert process.wait(timeout=30) == 0
        assert not connection.exists()
        supervisor = JobSupervisor({"jobs_dir": str(tmp_path / "jobs")}, start=False)
        supervisor.close()
    finally:
        if process is not None:
            if process.poll() is None:
                process.kill()
                process.wait()
            process.stderr.close()
