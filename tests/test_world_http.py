"""Loopback WorldPrompt HTTP protocol with real worker subprocesses."""
import http.client
import json
from pathlib import Path
import threading
import uuid

import pytest

from worldprompt_core import __version__
from worldprompt_core.world_jobs import JobSupervisor
from worldprompt_core.world_server import MOD_VERSION_HEADER, VERSION_HEADER, make_world_handler
from http.server import ThreadingHTTPServer

from test_world_jobs import isolated_worker, submission, wait_status


@pytest.fixture
def world_http(tmp_path):
    supervisor = JobSupervisor(
        {"jobs_dir": str(tmp_path / "jobs"), "job_limits": True, "world_concurrency": 2,
         "world_lease_seconds": 120}, worker_target=isolated_worker)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0),
                               make_world_handler(supervisor, "test-token"))
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()

    def request(method, path, body=None, auth="Bearer test-token", mod_version=__version__, **headers):
        connection = http.client.HTTPConnection("127.0.0.1", httpd.server_port, timeout=5)
        payload = json.dumps(body).encode() if body is not None else None
        if payload:
            headers.setdefault("Content-Type", "application/json")
        if auth is not None:
            headers.setdefault("Authorization", auth)
        if mod_version is not None:
            headers[MOD_VERSION_HEADER] = mod_version
        connection.request(method, path, body=payload, headers=headers)
        response = connection.getresponse()
        content = response.read()
        status = response.status
        assert response.getheader(VERSION_HEADER) == __version__
        connection.close()
        return status, json.loads(content) if content else None

    yield supervisor, request, httpd.server_port
    httpd.shutdown()
    httpd.server_close()
    thread.join(timeout=2)
    supervisor.close()


def test_world_routes_require_token_and_loopback_host(world_http):
    _, request, port = world_http
    for method, path, body in [
        ("GET", "/api/world/info", None),
        ("GET", "/api/world/jobs", None),
        ("POST", "/api/world/jobs", submission()),
    ]:
        for auth in (None, "test-token", "Bearer wrong"):
            status, result = request(method, path, body, auth=auth)
            assert status == 403
            assert result["error"]
        status, _ = request(method, path, body, Host=f"evil.example:{port}")
        assert status == 403
    assert request("GET", "/api/world/jobs")[1] == {"jobs": []}


def test_info_reports_current_settings_and_load_without_private_config(world_http):
    supervisor, request, _ = world_http
    with supervisor.lock:
        supervisor.config.update(codex_model="test-model", codex_reasoning_effort="high", codex_fast_mode=True,
                                 world_token_file="private-token-location", codex_command=["private-executable"])
    status, info = request("GET", "/api/world/info")
    assert status == 200
    assert info["version"] and info["provider"] == "codex"
    assert (info["model"], info["reasoning_effort"], info["fast_mode"]) == ("test-model", "high", True)
    assert info["active_jobs"] == info["queued_jobs"] == 0
    assert info["world_concurrency"] == 2 and info["max_selection_blocks"] == 4_000_000
    assert info["job_limits"] is True and info["timeout_seconds"] == 3600
    assert "private" not in json.dumps(info)
    assert request("GET", "/api/world/info", Origin="https://attacker.example")[0] == 403
    _, job = request("POST", "/api/world/jobs", submission())
    wait_status(supervisor, job["id"], "ready")
    assert request("GET", "/api/world/info")[1]["active_jobs"] == 1
    for details in [job, request("GET", f'/api/world/jobs/{job["id"]}')[1],
                    request("GET", f'/api/world/jobs/{job["id"]}/result')[1]]:
        assert (details["model"], details["reasoning_effort"], details["fast_mode"]) == ("test-model", "high", True)
        assert "_agent_config" not in details


def test_submit_poll_complete_result_reconstruct_and_finish(world_http):
    supervisor, request, _ = world_http
    status, job = request("POST", "/api/world/jobs", submission())
    assert status == 202
    job_id = str(uuid.UUID(job["id"]))
    wait_status(supervisor, job_id, "ready")
    prefix = f"/api/world/jobs/{job_id}"
    status, details = request("GET", prefix)
    assert status == 200 and details["status"] == "ready"
    assert details["owner_name"] == "Alice" and details["effort"] == "normal"
    current = request("GET", "/api/world/jobs")[1]["jobs"]
    assert [entry["id"] for entry in current] == [job_id]
    status, result = request("GET", prefix + "/result")
    assert status == 200
    assert result["dimensions"] == [3, 2, 2]
    assert len(result["plan"]["operations"]) == result["operation_count"] == 2
    import base64
    import struct
    assert list(struct.iter_unpack("<III", base64.b64decode(result["operations"][0]["runs_base64"]))) == [(0, 2, 1), (3, 2, 1)]
    assert list(struct.iter_unpack("<III", base64.b64decode(result["operations"][1]["runs_base64"]))) == [(1, 1, 0)]
    status, reconstructing = request("POST", prefix + "/application",
                                     {"status": "reconstructing", "operation": 0})
    assert status == 200 and reconstructing["status"] == "reconstructing"
    # The default per-player limit is 0 (none), so the same owner may start another job.
    status, concurrent = request("POST", "/api/world/jobs", submission())
    assert status == 202 and concurrent["owner_id"] == job["owner_id"] and concurrent["id"] != job_id
    status, _ = request("POST", prefix + "/application",
                        {"status": "reconstructing", "operation": 2})
    assert status == 200
    status, completed = request("POST", prefix + "/application",
                               {"status": "completed", "operation": 2})
    assert status == 200 and completed["status"] == "completed"
    assert [entry["id"] for entry in request("GET", "/api/world/jobs")[1]["jobs"]] == [concurrent["id"]]


def test_live_catalog_crosses_http_only_as_private_idempotent_job_input(world_http):
    supervisor, request_http, _ = world_http
    body = submission()
    body["block_catalog"] = json.loads((Path(__file__).parent / "fixtures" / "block_catalog.json").read_text(encoding="utf-8"))
    status, job = request_http("POST", "/api/world/jobs", body)
    assert status == 202 and "block_catalog" not in job
    stored = json.loads((supervisor.directory / job["id"] / "request.json").read_text())
    assert stored["block_catalog"] == body["block_catalog"]
    assert request_http("POST", "/api/world/jobs", json.loads(json.dumps(body)))[1]["id"] == job["id"]
    changed = json.loads(json.dumps(body))
    changed["block_catalog"]["minecraft_version"] = "different"
    status, error = request_http("POST", "/api/world/jobs", changed)
    assert status == 400 and "different submission data" in error["error"]
    assert "block_catalog" not in supervisor.get_job(job["id"])


def test_per_player_limit_is_a_409_with_a_stable_code(world_http):
    supervisor, request, _ = world_http
    supervisor.max_jobs_per_player = 2
    assert request("GET", "/api/world/info")[1]["world_max_jobs_per_player"] == 2
    first = request("POST", "/api/world/jobs", submission(prompt="blocked"))[1]
    second = request("POST", "/api/world/jobs", submission(prompt="blocked"))[1]
    listed = request("GET", "/api/world/jobs")[1]["jobs"]
    assert {entry["id"] for entry in listed} == {first["id"], second["id"]}
    assert {entry["owner_id"] for entry in listed} == {first["owner_id"]}
    status, rejected = request("POST", "/api/world/jobs", submission(prompt="third"))
    assert status == 409
    assert rejected["code"] == "player_job_limit" and rejected["limit"] == 2
    assert "at most 2 active WorldPrompt jobs per player" in rejected["error"]
    from test_world_jobs import OWNER_B
    assert request("POST", "/api/world/jobs", submission(OWNER_B))[0] == 202


def test_unready_result_cannot_escape_and_cancel_frees_player(world_http):
    supervisor, request, _ = world_http
    status, job = request("POST", "/api/world/jobs", submission(prompt="blocked"))
    assert status == 202
    wait_status(supervisor, job["id"], "planning")
    prefix = f'/api/world/jobs/{job["id"]}'
    assert request("GET", prefix + "/result")[0] == 400
    status, _ = request("POST", prefix + "/cancel", {"message": "Player disconnected"})
    assert status == 200
    wait_status(supervisor, job["id"], "failed")
    assert request("POST", "/api/world/jobs", submission())[0] == 202


def test_invalid_inputs_and_unknown_jobs_are_structured_errors(world_http):
    _, request, _ = world_http
    assert request("POST", "/api/world/jobs", submission(effort="high-ish"))[0] == 400
    status, unknown = request("GET", f"/api/world/jobs/{uuid.uuid4()}")
    assert status == 404 and unknown["error"]
    assert request("GET", "/api/world/jobs/../../config.local.json")[0] == 404
    assert request("GET", "/api/world/jobs/not-a-uuid/result")[0] == 404
    status, job = request("POST", "/api/world/jobs", submission())
    assert status == 202
    prefix = f'/api/world/jobs/{job["id"]}'
    assert request("POST", prefix + "/application", {"status": "planning"})[0] == 400


def test_removed_routes_are_not_found(world_http):
    _, request, _ = world_http
    status, job = request("POST", "/api/world/jobs", submission())
    assert status == 202
    prefix = f'/api/world/jobs/{job["id"]}'
    assert request("GET", prefix + "/operations/0")[0] == 404
    assert request("GET", prefix + "/operations/0?offset=banana")[0] == 404
    assert request("POST", prefix + "/rebuild", {})[0] == 404


def compatible_version(change):
    """__version__ with MAJOR, MINOR or PATCH moved by one: only a PATCH change stays compatible."""
    parts = [int(part) for part in __version__.split(".")[:3]]
    parts[change] += 1
    return ".".join(map(str, parts))


def test_mod_must_share_cores_major_and_minor_version(world_http):
    _, request, _ = world_http
    status, result = request("GET", "/api/world/jobs", mod_version=compatible_version(2))
    assert status == 200 and result == {"jobs": []}
    for mod_version, expected in ((None, "does not report its version"), ("banana", "unreadable version"),
                                  (compatible_version(1), "must match"), (compatible_version(0), "must match")):
        for method, path, body in (("GET", "/api/world/jobs", None), ("POST", "/api/world/jobs", submission())):
            status, result = request(method, path, body, mod_version=mod_version)
            assert status == 409
            assert result["code"] == "version_mismatch" and result["core_version"] == __version__
            assert expected in result["error"]
    # /wp info stays readable, so a player can see both versions.
    status, info = request("GET", "/api/world/info", mod_version=None)
    assert status == 200 and info["version"] == __version__


@pytest.mark.parametrize("extra,status", [
    ({"Authorization": "Bearer wrong"}, 403),
    ({"Origin": "https://attacker.example"}, 403),
    ({"Content-Type": "text/plain"}, 415),
])
def test_rejected_post_with_split_headers_and_body_keeps_json_error(world_http, extra, status):
    import socket
    import time
    _, _, port = world_http
    body = b"Ignored request body; rejection must not parse this JSON."
    headers = {"Host": f"127.0.0.1:{port}", "Content-Length": str(len(body)),
               "Content-Type": "application/json", "Authorization": "Bearer test-token",
               MOD_VERSION_HEADER: __version__, **extra}
    request = "POST /api/world/jobs HTTP/1.1\r\n" + "".join(f"{key}: {value}\r\n" for key, value in headers.items()) + "\r\n"
    with socket.create_connection(("127.0.0.1", port), timeout=3) as connection:
        connection.sendall(request.encode("ascii"))
        # Force headers/body into separate arrivals. Closing on the headers
        # alone previously raced the client's send and reset Windows sockets.
        time.sleep(.05)
        connection.sendall(body)
        response = http.client.HTTPResponse(connection)
        response.begin()
        assert response.status == status
        assert json.loads(response.read())["error"]


def test_rejected_request_does_not_wait_for_oversized_body(world_http):
    import socket
    from worldprompt_core.world_server import MAX_WORLD_BODY
    _, _, port = world_http
    request = (f"POST /api/world/jobs HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n"
               f"Content-Length: {MAX_WORLD_BODY + 1}\r\nContent-Type: application/json\r\n"
               f"Authorization: Bearer test-token\r\n{MOD_VERSION_HEADER}: {__version__}\r\n\r\n")
    with socket.create_connection(("127.0.0.1", port), timeout=2) as connection:
        connection.sendall(request.encode("ascii"))
        response = http.client.HTTPResponse(connection)
        response.begin()
        assert response.status == 413
        assert json.loads(response.read())["error"]


def test_rejected_body_drain_deadline_holds_for_a_trickling_sender():
    import socket
    import time
    from types import SimpleNamespace
    from worldprompt_core.http_utils import discard_rejected_body
    receive, send = socket.socketpair()
    stream = receive.makefile("rb")
    stop = threading.Event()
    def trickle():
        try:
            while not stop.is_set():
                send.sendall(b"x")
                stop.wait(.03)
        except OSError:
            pass
    sender = threading.Thread(target=trickle, daemon=True)
    sender.start()
    handler = SimpleNamespace(headers={"Content-Length": "8192"}, connection=receive, rfile=stream)
    try:
        started = time.monotonic()
        discard_rejected_body(handler, 64000)
        # The sender trickles for minutes; the drain gives up after its own ~1 second deadline.
        assert time.monotonic() - started < 8
        assert handler.body_read
    finally:
        stop.set()
        sender.join(timeout=2)
        stream.close()
        receive.close()
        send.close()


def test_only_a_missing_job_is_a_404_and_other_lookup_errors_are_not(world_http, monkeypatch):
    supervisor, request, _ = world_http
    status, job = request("POST", "/api/world/jobs", submission())
    assert status == 202
    assert request("GET", f'/api/world/jobs/{uuid.uuid4()}/result')[0] == 404
    assert request("POST", f'/api/world/jobs/{uuid.uuid4()}/cancel', {})[0] == 404

    def broken(job_id):
        raise KeyError("operation_count")

    monkeypatch.setattr(supervisor, "get_job", broken)
    status, error = request("GET", f'/api/world/jobs/{job["id"]}')
    assert status == 500 and "operation_count" not in error["error"]
    assert "not found" not in error["error"].lower()


# What the mod's `/wp jobs` listing and stage text read from each list entry
# (PromptManager.collectRemoteJobs, remoteStage, modelLabel).
MOD_LIST_FIELDS = {"id", "owner_id", "owner_name", "effort", "model", "reasoning_effort", "fast_mode",
                   "status", "queue_position", "operation", "operation_count"}


def test_job_list_drops_what_the_mod_never_reads_while_the_gui_keeps_it(world_http):
    supervisor, request, _ = world_http
    job = request("POST", "/api/world/jobs", submission(prompt="Long prompt " * 100, lecterns=[]))[1]
    wait_status(supervisor, job["id"], "ready")
    entry = request("GET", "/api/world/jobs")[1]["jobs"][0]
    assert MOD_LIST_FIELDS <= set(entry)
    assert {"prompt", "lecterns", "summary", "selection_origin", "dimensions"}.isdisjoint(entry)
    # The single-job route and the GUI's own list are unchanged.
    assert request("GET", f'/api/world/jobs/{job["id"]}')[1]["prompt"].startswith("Long prompt")
    gui_entry = supervisor.list_jobs()[0]
    assert gui_entry["prompt"].startswith("Long prompt") and gui_entry["lecterns"] == []
    assert gui_entry["dimensions"] == [3, 2, 2]
    assert len(json.dumps(entry)) < len(json.dumps(gui_entry))


def test_result_stays_available_after_completion(world_http):
    supervisor, request, _ = world_http
    job = request("POST", "/api/world/jobs", submission())[1]
    wait_status(supervisor, job["id"], "ready")
    prefix = f'/api/world/jobs/{job["id"]}'
    assert request("GET", prefix + "/result")[0] == 200
    request("POST", prefix + "/application", {"status": "reconstructing", "operation": 2})
    assert request("POST", prefix + "/application", {"status": "completed", "operation": 2})[0] == 200
    assert request("GET", prefix + "/result")[0] == 200
    # Acknowledging again stays idempotent for a mod retrying a lost response.
    assert request("POST", prefix + "/application", {"status": "completed", "operation": 2})[0] == 200


def test_mod_uses_the_same_version_headers():
    from pathlib import Path
    versions = (Path(__file__).resolve().parents[1] / "pure/src/main/java/dev/worldprompt/kernel/Versions.java").read_text(encoding="utf-8")
    assert f'CORE_HEADER = "{VERSION_HEADER}"' in versions
    assert f'MOD_HEADER = "{MOD_VERSION_HEADER}"' in versions
    assert 'MISMATCH = "version_mismatch"' in versions
