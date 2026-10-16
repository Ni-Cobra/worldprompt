"""Verify a built Core, including a real frozen worker, without any AI account."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import socket
import struct
import subprocess
import sys
import tempfile
import time
from urllib.error import URLError
from urllib.request import Request, urlopen
import uuid


# Only this deterministic CLI fixture runs. No installed agent is invoked.
FAKE_CLI = '''import base64, json, pathlib, sys, subprocess
args = sys.argv[1:]
if "app-server" in args:
    for line in sys.stdin:
        message = json.loads(line)
        if message["method"] == "initialized":
            continue
        result = {} if message["method"] == "initialize" else {"data": [
            {"model": "gpt-6-astra", "displayName": "GPT-6-Astra", "inputModalities": ["text", "image"]},
            {"model": "fixture-model", "displayName": "Fixture"}], "nextCursor": None}
        print(json.dumps({"id": message["id"], "result": result}), flush=True)
    raise SystemExit(0)
if args[-2:] == ["auth", "status"]:
    print(json.dumps({"loggedIn": True, "authMethod": "claude.ai"}))
    raise SystemExit(0)
if "--output-last-message" not in args and "--input-format" not in args:
    print("fixture authenticated")
    raise SystemExit(0)
request = json.loads((sys.stdin.buffer.readline() if "--input-format" in args
                      else sys.stdin.buffer.read()).decode("utf-8"))
if request.get("type") == "control_request":
    assert request["request"]["subtype"] == "initialize"
    print(json.dumps({"type": "control_response", "response": {"subtype": "success",
        "request_id": request["request_id"], "response": {"models": [
            {"value": "opus", "displayName": "Opus"}, {"value": "sonnet", "displayName": "Sonnet"}]}}}), flush=True)
    sys.stdin.buffer.read()
    raise SystemExit(0)
expected_fast = args[args.index("--fixture-fast") + 1] == "true"
if "--input-format" in args:
    assert args[args.index("--effort") + 1] == "high"
    assert json.loads(args[args.index("--settings") + 1])["fastMode"] == expected_fast
else:
    assert 'model_reasoning_effort="high"' in args
    assert ('service_tier="fast"' in args) == expected_fast
plan = {"version": 1, "summary": "Packaged worker fixture", "operations": [
    {"op": "fill", "min": [1, 1, 1], "max": [1, 1, 1], "block": "minecraft:stone"}
]}
# Exercise the exact MCP child command emitted by the frozen worker.
if "--mcp-config" in args:
    server = json.loads(args[args.index("--mcp-config") + 1])["mcpServers"]["worldprompt"]
else:
    server = {}
    for i, value in enumerate(args):
        if value == "-c" and args[i + 1].startswith("mcp_servers.worldprompt."):
            key, value = args[i + 1].split("=", 1)
            server[key.rsplit(".", 1)[1]] = json.loads(value)
messages = [
    {"jsonrpc": "2.0", "id": 1, "method": "initialize"},
    {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "get_block", "arguments": {"block": "minecraft:oak_stairs"}}},
    {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "validate_plan", "arguments": {"plan": plan}}},
    {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "inspect_region", "arguments": {"min": [1, 1, 1], "max": [1, 1, 1]}}},
    {"jsonrpc": "2.0", "id": 5, "method": "tools/call", "params": {"name": "preview_plan", "arguments": {"plan": plan, "include_image": True}}},
    {"jsonrpc": "2.0", "id": 6, "method": "tools/call", "params": {"name": "inspect_region", "arguments": {"min": [1, 1, 1], "max": [1, 1, 1], "target": "candidate"}}},
]
result = subprocess.run([server["command"], *server["args"]],
                        input="\\n".join(json.dumps(m) for m in messages) + "\\n",
                        capture_output=True, text=True, check=True, timeout=30)
responses = [json.loads(line) for line in result.stdout.splitlines()]
assert json.loads(responses[1]["result"]["content"][0]["text"])["properties"]["facing"]
assert json.loads(responses[2]["result"]["content"][0]["text"])["valid"]
assert json.loads(responses[3]["result"]["content"][0]["text"])["palette"] == ["minecraft:air"]
assert json.loads(responses[4]["result"]["content"][0]["text"])["fit_diagnostics"]["changed_voxels"] == 1
assert responses[4]["result"]["content"][1]["type"] == "image"
assert json.loads(responses[5]["result"]["content"][0]["text"])["palette"] == ["minecraft:stone"]
if "--input-format" in args:
    images = [part for part in request["message"]["content"] if part["type"] == "image"]
    assert images and all(base64.b64decode(part["source"]["data"]).startswith(bytes([137,80,78,71])) for part in images)
    print(json.dumps({"type": "result", "subtype": "success", "is_error": False,
                      "structured_output": plan, "usage": {"input_tokens": 1, "output_tokens": 1}}))
else:
    output = pathlib.Path(args[args.index("--output-last-message") + 1])
    output.write_text(json.dumps(plan), encoding="utf-8")
    print(json.dumps({"type": "turn.completed", "usage": {"input_tokens": 1, "output_tokens": 1}}))
'''


def free_ports():
    # Hold both sockets while choosing distinct ports, then release for Core.
    with socket.socket() as gui, socket.socket() as world:
        gui.bind(("127.0.0.1", 0))
        world.bind(("127.0.0.1", 0))
        return gui.getsockname()[1], world.getsockname()[1]


# Adapter requests claim a mod version matching the Core under test, set once it reports its version.
WORLD_HEADERS = {}


def http(url, *, body=None, token=None, session=None, as_json=True):
    headers = {}
    data = None
    if body is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(body).encode("utf-8")
    if token is not None:
        headers["Authorization"] = "Bearer " + token
        headers.update(WORLD_HEADERS)
    if session is not None:
        headers["X-WorldPrompt-Session"] = session
    with urlopen(Request(url, data=data, headers=headers), timeout=5) as response:
        content = response.read()
        return json.loads(content) if as_json else content


def verify_job(endpoint, token, expected_settings):
    job = http(endpoint, token=token, body={
        "request_id": str(uuid.uuid4()), "owner_id": str(uuid.uuid4()),
        "owner_name": "PackagingSmoke", "prompt": "Place a single stone block.",
        "effort": "normal", "source": {"dimensions": [3, 3, 3],
        "palette": ["minecraft:air"], "blocks": [0] * 27, "data_version": 4901},
    })
    assert all(job.get(key) == value for key, value in expected_settings.items()), job
    job_url = endpoint + "/" + job["id"]
    deadline = time.monotonic() + 45
    while True:
        job = http(job_url, token=token)
        if job["status"] == "ready":
            break
        assert job["status"] not in {"failed", "cancelled"}, job
        assert time.monotonic() < deadline, job
        time.sleep(.2)
    result = http(job_url + "/result", token=token)
    assert all(result.get(key) == value for key, value in expected_settings.items()), result
    assert result["operation_count"] == 1, result
    import base64
    import struct
    assert result["result_version"] == 2 and result["run_encoding"] == "base64-u32le"
    runs = list(struct.iter_unpack("<III", base64.b64decode(result["operations"][0]["runs_base64"])))
    assert runs == [(13, 1, 1)], runs
    http(job_url + "/application", token=token,
         body={"status": "reconstructing", "operation": 0})
    http(job_url + "/application", token=token,
         body={"status": "completed", "operation": 1})


def smoke(command):
    console_command = list(command)
    if sys.platform == "win32" and Path(command[0]).name == "WorldPrompt-Core.exe":
        console_command[0] = str(Path(command[0]).with_name("WorldPrompt-Core-Console.exe"))
        for executable, subsystem in ((command[0], 2), (console_command[0], 3)):
            image = Path(executable).read_bytes()
            pe = struct.unpack_from("<I", image, 0x3c)[0]
            assert struct.unpack_from("<H", image, pe + 24 + 68)[0] == subsystem
    for argument, expected in (("--help", "WorldPrompt"), ("--version", "WorldPrompt Core")):
        result = subprocess.run([*console_command, argument], capture_output=True, text=True,
                                check=True, timeout=30)
        assert expected in result.stdout, result.stdout
        if argument == "--version":
            WORLD_HEADERS["X-WorldPrompt-Mod-Version"] = result.stdout.split()[-1]
    with tempfile.TemporaryDirectory(prefix="worldprompt-smoke-") as temporary:
        directory = Path(temporary)
        gui_port, world_port = free_ports()
        config = directory / "config.json"
        fake = directory / "fake_cli.py"
        fake.write_text(FAKE_CLI, encoding="utf-8")
        config.write_text(json.dumps({
            "jobs_dir": str(directory / "jobs"), "provider": "codex",
            "codex_command": [sys.executable, str(fake)], "claude_command": [sys.executable, str(fake)],
            "port": gui_port, "world_port": world_port, "timeout_seconds": 30,
        }), encoding="utf-8")
        connection_file = directory / "connection.json"
        environment = dict(os.environ, WORLDPROMPT_CONFIG=str(config),
                           WORLDPROMPT_CONNECTION_FILE=str(connection_file))
        # Never let the source checkout mask a missing packaged import.
        environment.pop("PYTHONPATH", None)
        if sys.platform == "win32":
            environment["LOCALAPPDATA"] = str(directory / "localappdata")
        gui_url = f"http://127.0.0.1:{gui_port}"
        access_file = config.with_name(config.name + ".gui-access.json")
        with (directory / "core.log").open("w+", encoding="utf-8") as log:
            process = subprocess.Popen([*command, "--config", str(config), "serve", "--no-browser"],
                                       cwd=directory, env=environment,
                                       stdout=None if sys.platform == "win32" else log,
                                       stderr=None if sys.platform == "win32" else subprocess.STDOUT,
                                       creationflags=0x8 if sys.platform == "win32" else 0)
            try:
                deadline = time.monotonic() + 45
                while True:
                    if process.poll() is not None:
                        raise AssertionError("Packaged Core exited before becoming ready")
                    try:
                        session = json.loads(access_file.read_text(encoding="utf-8"))["session"]
                        state = http(gui_url + "/api/state", session=session)
                        break
                    except (URLError, TimeoutError, ConnectionError, FileNotFoundError):
                        if time.monotonic() >= deadline:
                            raise AssertionError("Packaged Core did not become ready")
                        time.sleep(.2)
                assert state["product"] == "WorldPrompt Core", state
                if sys.platform == "win32":
                    # Explorer-style launch, with no inherited console or streams.
                    duplicate = subprocess.Popen([*command, "--config", str(config), "serve", "--no-browser"],
                                                 cwd=directory, env=environment, creationflags=0x8)
                    try:
                        assert duplicate.wait(timeout=10) == 0
                    finally:
                        if duplicate.poll() is None:
                            duplicate.kill()
                            duplicate.wait()
                    assert process.poll() is None
                for route in ("/", "/app.js", "/style.css", "/icon.svg", "/icon.png", "/fonts/public-sans-latin.woff2"):
                    assert len(http(gui_url + route, as_json=False)) > 100, route
                page = http(gui_url + "/", as_json=False).decode("utf-8")
                assert "Settings and activity" in page and "A little imagination" not in page
                assert "codex_fast_mode" in page and "claude_reasoning_effort" in page
                discovered = json.loads(connection_file.read_text(encoding="utf-8"))
                assert discovered["serviceUrl"] == f"http://127.0.0.1:{world_port}", discovered
                token_path = Path(discovered["tokenFile"])
                assert token_path.is_absolute() and token_path.is_file()
                token = token_path.read_text(encoding="utf-8").strip()
                assert len(token) >= 24
                endpoint = discovered["serviceUrl"] + "/api/world/jobs"
                assert http(endpoint, token=token) == {"jobs": []}
                for provider in ("codex", "claude"):
                    for fast_mode in (True, False):
                        model = "gpt-6-astra" if provider == "codex" else "opus"
                        settings = dict(state["config"], provider=provider)
                        settings.update({provider + "_model": model, provider + "_reasoning_effort": "high",
                                         provider + "_fast_mode": fast_mode,
                                         provider + "_command": [sys.executable, str(fake), "--fixture-fast", str(fast_mode).lower()]})
                        state = http(gui_url + "/api/config", session=session, body=settings)
                        assert state["config"]["provider"] == provider
                        state = http(gui_url + "/api/providers/models", session=session, body={"provider": provider})
                        model_deadline = time.monotonic() + 25
                        while state["models"][provider]["loading"]:
                            assert time.monotonic() < model_deadline, state["models"]
                            time.sleep(.2)
                            state = http(gui_url + "/api/state", session=session)
                        choices = state["models"][provider]
                        assert not choices.get("error") and any(item["id"] == model for item in choices["items"]), choices
                        assert json.loads(config.read_text(encoding="utf-8"))[provider + "_fast_mode"] == fast_mode
                        expected = {"provider": provider, "model": model, "reasoning_effort": "high", "fast_mode": fast_mode}
                        info = http(discovered["serviceUrl"] + "/api/world/info", token=token)
                        assert all(info.get(key) == value for key, value in expected.items()), info
                        verify_job(endpoint, token, expected)
                http(gui_url + "/api/shutdown", session=session, body={})
                assert process.wait(timeout=15) == 0
                assert not connection_file.exists()
                print("Core smoke passed: launcher, GUI, discovery, info, both providers, effort/fast settings, worker/replay and graceful exit.")
            except BaseException:
                desktop_log = directory / "localappdata/WorldPrompt/logs/core.log"
                if desktop_log.exists():
                    print(desktop_log.read_text(encoding="utf-8")[-12000:], file=sys.stderr)
                log.flush()
                log.seek(0)
                print(log.read()[-12000:], file=sys.stderr)
                raise
            finally:
                if process.poll() is None:
                    process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=10)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("executable", type=Path, help="Built WorldPrompt-Core executable")
    arguments = parser.parse_args()
    smoke([str(arguments.executable.resolve())])
