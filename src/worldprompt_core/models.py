"""Discover CLI model choices without submitting a prompt or starting a model turn."""
from __future__ import annotations

from contextlib import contextmanager
import json
import os
import queue
import signal
import subprocess
import tempfile
import threading
import time

from .agent_types import AgentConfig
from .cli_discovery import resolve_command
from .cli_process import windows_cli_dll_directory
from .providers import get_provider, provider_environment


class ModelListError(ValueError):
    pass


@contextmanager
def _connection(config: AgentConfig, timeout: float):
    args = get_provider(config.provider).model_list_command(resolve_command(config.provider, config.command))
    with tempfile.TemporaryDirectory(prefix="worldprompt-models-") as directory:
        with windows_cli_dll_directory():
            process = subprocess.Popen(args, cwd=directory, env=provider_environment(config),
                                       stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                       **({"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {"start_new_session": True}))
        messages = queue.Queue()
        deadline = time.monotonic() + timeout

        def read():
            total = 0
            try:
                for _ in range(1024):
                    line = process.stdout.readline(256_001)
                    if not line:
                        break
                    total += len(line)
                    if len(line) > 256_000 or total > 2_000_000:
                        raise ModelListError("The CLI model response exceeded the size limit.")
                    try:
                        value = json.loads(line)
                    except ValueError:
                        continue
                    if isinstance(value, dict):
                        messages.put(value)
            except (OSError, ValueError):
                messages.put(ModelListError("Could not read the CLI model list."))
            finally:
                process.stdout.close()
                messages.put(ModelListError("The CLI closed before returning its model list."))

        reader = threading.Thread(target=read, name="model-list-output", daemon=True)
        reader.start()

        def send(value):
            process.stdin.write((json.dumps(value) + "\n").encode("utf-8"))
            process.stdin.flush()

        def receive(matches):
            while True:
                try:
                    event = messages.get(timeout=max(0, deadline - time.monotonic()))
                except queue.Empty:
                    raise ModelListError("The CLI model list timed out. Try Refresh models again.") from None
                if isinstance(event, Exception):
                    raise event
                if matches(event):
                    return event

        try:
            yield send, receive
        finally:
            # A CLI may leave subprocesses holding stdout open even after its
            # main process exits. Stop the whole private POSIX group before
            # joining the reader; closing a buffered pipe while it is blocked
            # in readline would otherwise deadlock model refresh indefinitely.
            if os.name == "posix":
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            elif process.poll() is None:
                try:
                    subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                   timeout=3, creationflags=subprocess.CREATE_NO_WINDOW)
                except (OSError, subprocess.SubprocessError):
                    process.kill()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=3)
            process.stdin.close()
            reader.join(timeout=1)
            if not reader.is_alive():
                process.stdout.close()


def _text(value, limit=128):
    return isinstance(value, str) and bool(value.strip()) and len(value) <= limit and not any(ord(c) < 32 for c in value)


def list_models(config: AgentConfig, *, timeout=20) -> list[dict]:
    """Return the configured CLI's picker choices, including paginated Codex results.

    Only public model fields leave this boundary. Claude's initialization also
    includes account information, which is deliberately discarded.
    """
    raw = []
    with _connection(config, timeout) as (send, receive):
        if config.provider == "codex":
            send({"method": "initialize", "id": 0, "params": {
                "clientInfo": {"name": "worldprompt", "version": "1"}}})
            if "error" in receive(lambda event: event.get("id") == 0):
                raise ModelListError("Codex could not initialize its model list. Check your CLI version and sign-in.")
            send({"method": "initialized"})
            cursor, seen = None, set()
            for request_id in range(1, 21):
                send({"method": "model/list", "id": request_id,
                      "params": {"limit": 100, "cursor": cursor, "includeHidden": False}})
                response = receive(lambda event: event.get("id") == request_id)
                result = response.get("result", {})
                if "error" in response or not isinstance(result, dict) or not isinstance(result.get("data"), list):
                    raise ModelListError("Codex could not list models. Check your CLI version and sign-in.")
                raw.extend(result["data"])
                cursor = result.get("nextCursor")
                if cursor is None:
                    break
                if not _text(cursor, 4096) or cursor in seen:
                    raise ModelListError("Codex returned an invalid model-list cursor.")
                seen.add(cursor)
            else:
                raise ModelListError("Codex returned too many model-list pages.")
        else:
            send({"type": "control_request", "request_id": "models",
                  "request": {"subtype": "initialize", "hooks": {}}})
            response = receive(lambda event: event.get("type") == "control_response"
                               and isinstance(event.get("response"), dict)
                               and event["response"].get("request_id") == "models")["response"]
            result = response.get("response", {})
            if response.get("subtype") != "success" or not isinstance(result, dict) or not isinstance(result.get("models"), list):
                raise ModelListError("Claude could not list models. Check your CLI version and sign-in.")
            raw = result["models"]
    provider = get_provider(config.provider)
    models = {}
    for item in raw:
        if not isinstance(item, dict) or item.get("hidden") is True:
            continue
        model = item.get(provider.model_key)
        if not _text(model):
            continue
        label = item.get("displayName")
        modalities = item.get("inputModalities")
        models.setdefault(model, {"id": model, "name": label if _text(label, 200) else model,
                                  "supports_images": not isinstance(modalities, list) or "image" in modalities,
                                  "reasoning_efforts": provider.model_efforts(item)})
        if _text(item.get("resolvedModel")):
            models[model]["resolved_model"] = item["resolvedModel"]
        default = item.get("defaultReasoningEffort")
        if default in models[model]["reasoning_efforts"]:
            models[model]["default_reasoning_effort"] = default
    return list(models.values())
