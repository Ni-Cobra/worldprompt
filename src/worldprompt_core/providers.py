"""Codex and Claude CLI protocols behind one small ``Provider`` interface.

Everything that differs between the two CLIs lives here: the planning command, the
request fed on stdin, how a final plan and its token usage are read from the event
stream, how a failed run is classified, the login-status command and its output,
the model-picker command and the environment tweaks. ``agent`` drives the bounded
subprocess generically through a ``Provider``; authentication stays inside the
installed CLIs (no API SDK, no key storage).

Claude uses its documented streaming input for base64 images and the final
``structured_output`` field for plans. Only private offline planner tools are exposed.
"""
from __future__ import annotations

import base64
import json
import os
from pathlib import Path
import re
import sys
from typing import Any, Sequence

from . import wsl
from .agent_types import AgentConfig, AgentError, is_reasoning_effort
from .cli_discovery import resolve_command
from .limits import MAX_CLAUDE_TURNS
from .operations import plan_schema
from .planner_tools import TOOL_NAMES, server_command


# These variables can silently switch a CLI from subscription sign-in to API
# billing, a third-party host, or a nested coding session. Keep normal OS and CLI
# home variables so the official executables can find their own saved login.
_API_ENV = {
    "OPENAI_API_KEY", "OPENAI_BASE_URL", "CODEX_API_KEY",
    "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL",
    "CLAUDE_CODE_OAUTH_TOKEN", "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX",
    "CLAUDE_CODE_USE_FOUNDRY", "AWS_BEARER_TOKEN_BEDROCK", "CLAUDECODE",
    "CLAUDE_CODE_SIMPLE", "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_CODE_SAFE_MODE",
}


def provider_environment(config: AgentConfig) -> dict[str, str]:
    """Use CLI sign-in and keep frozen-app libraries out of external programs.

    Builds an environment only: it never changes the calling process. Spawns
    wrap themselves in ``cli_process.windows_cli_dll_directory()`` for the DLL search path.
    """
    result = {key: value for key, value in os.environ.items()
              if key.upper() not in _API_ENV and not key.upper().startswith("ANTHROPIC_FOUNDRY_")}
    for key in ("LD_LIBRARY_PATH", "LIBPATH"):
        if key + "_ORIG" in result:
            result[key] = result.pop(key + "_ORIG")
        elif getattr(sys, "frozen", False):
            result.pop(key, None)
    result.update(get_provider(config.provider).extra_environment)
    return result


# --- Failure classification (shared by both CLIs; only the login hint differs) ---

_AUTH_STATUS = {401}
_LIMIT_STATUS = {429}
# Word-ish boundaries that treat "_" as a separator, so structured codes such as
# "usage_limit_exceeded" match while "oauth", "author" or UUID fragments do not.
_B, _E = r"(?<![a-z0-9])", r"(?![a-z0-9])"
_LIMIT_RE = re.compile(_B + r"(?:rate[ _-]?limit(?:ed)?|usage[ _-]?limit|quota)" + _E)
_AUTH_RE = re.compile(_B + r"(?:unauthori[sz]ed|authentication|not logged in|not signed in|not authenticated|invalid api key|please run /login"
                      r"|(?:http|status|error|code)[ :=_-]{0,3}401)" + _E)
_MODEL_RE = re.compile(_B + r"model" + _E)
_MODEL_PROBLEM_RE = re.compile(_B + r"(?:not supported|not found|does not exist|unavailable)" + _E)
_INCOMPATIBLE_RE = re.compile(_B + r"(?:unknown option|unexpected argument|unrecognized option|unknown feature)" + _E)
_SCHEMA_RE = re.compile(_B + r"schema" + _E)


def _event_errors(events: Path) -> tuple[list[str], set[int]]:
    """Error texts and HTTP-style status codes from the JSON event stream's error fields only.

    Codex JSONL reports failures as ``error`` / ``turn.failed`` events; Claude
    stream-json uses an ``error`` string on assistant/system events and an
    ``is_error`` result. Everything else in the stream (tool output, plans,
    token counts, ids) is data and never classified.
    """
    size = events.stat().st_size
    with events.open("rb") as handle:
        handle.seek(max(0, size - 512_000))
        lines = handle.read(512_000).decode("utf-8", errors="replace").splitlines()
    if size > 512_000:
        lines = lines[1:]  # The first line is probably cut in half.
    texts, statuses = [], set()
    def add_text(value):
        if isinstance(value, str):
            texts.append(value)
    def add_status(value):
        if type(value) is int:
            statuses.add(value)
    for line in lines:
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if not isinstance(event, dict):
            continue
        kind = event.get("type")
        error = event.get("error")
        if isinstance(error, dict):
            for key in ("message", "code", "type"):
                add_text(error.get(key))
            add_status(error.get("status"))
            add_status(error.get("status_code"))
        else:
            add_text(error)
        for key in ("api_error_status", "error_status"):
            add_status(event.get(key))
        if kind in ("error", "turn.failed"):
            add_text(event.get("message"))
        if kind == "result" and event.get("is_error") is True:
            add_text(event.get("result"))
            if isinstance(event.get("errors"), list):
                for item in event["errors"]:
                    add_text(item)
    return texts, statuses


def _classify_failure(stderr: Path, events: Path, login_command: str) -> str:
    """Classify known failures without returning raw logs or possible auth details.

    Only stderr lines and the structured error fields of the JSON event stream
    are examined, using word-boundary patterns, so ids, token counts and plan
    data in the event stream cannot masquerade as an auth or model failure.
    """
    def tail(path):
        with path.open("rb") as handle:
            handle.seek(max(0, path.stat().st_size - 16_000))
            return handle.read(16_000).decode("utf-8", errors="replace")
    texts, statuses = _event_errors(events)
    chunks = [line.lower() for line in tail(stderr).splitlines() + texts]
    def seen(pattern):
        return any(pattern.search(chunk) for chunk in chunks)
    if _LIMIT_STATUS & statuses or seen(_LIMIT_RE):
        return "The account reached a usage limit. Retry after its reset."
    if _AUTH_STATUS & statuses or seen(_AUTH_RE):
        return f"Run {login_command} using the configured executable."
    if any(_MODEL_RE.search(chunk) and _MODEL_PROBLEM_RE.search(chunk) for chunk in chunks):
        return "The configured model is unavailable for this CLI account."
    if seen(_INCOMPATIBLE_RE):
        return "The installed CLI is incompatible. Update the CLI and check its configured command."
    if seen(_SCHEMA_RE):
        return "The CLI rejected the structured output schema; check CLI compatibility."
    return "Inspect the private job logs for details; no world blocks were changed."


# --- Providers ---

def model_args(config: AgentConfig) -> list[str]:
    """Select the configured model; without one the CLI uses its own default."""
    return ["--model", config.model] if config.model else []


class Provider:
    """What Core needs from one CLI. Subclasses are stateless singletons (see ``get_provider``)."""

    name: str                       # Config / AgentConfig.provider value.
    label: str                      # Human name used in messages.
    login_command: str              # What the user runs to sign in; shown in auth hints.
    status_args: tuple[str, ...]    # Appended to the CLI command to read login status.
    keeps_stdin_open = False        # True when the CLI streams: stdin closes after its final event.
    extra_environment: dict[str, str] = {}
    model_key = "model"             # Field naming a model in the model picker's items.

    def build_command(self, job_dir: Path, config: AgentConfig, preview_paths: Sequence[Path] = ()) -> list[str]:
        """The full planning command line for one job directory."""
        raise NotImplementedError

    def prepare_input(self, job_dir: Path, request: str, preview_paths: Sequence[Path]) -> Path:
        """Return the file whose bytes are fed to the CLI's stdin."""
        raise NotImplementedError

    def is_final_event(self, event: Any) -> bool:
        """True for the stream event after which a streaming CLI needs no more input."""
        return False

    def read_result(self, events_path: Path, config: AgentConfig) -> tuple[dict | None, dict[str, int]]:
        """``(plan, usage)`` from the saved event stream.

        ``plan`` is None when the CLI writes ``plan.json`` itself. Usage is
        normalized so that ``input_tokens`` is the total input of the pass.
        """
        raise NotImplementedError

    def login_status(self, completed) -> tuple[bool, str | None]:
        """Interpret a finished status command: ``(ready, message)``.

        A non-None message is a definitive answer that replaces the generic ready/not-ready text.
        """
        return completed.returncode == 0, None

    def failure_detail(self, stderr: Path, events: Path) -> str:
        """Classify a failed run from stderr and the event stream without exposing raw logs."""
        return _classify_failure(stderr, events, self.login_command)

    def model_list_command(self, command: Sequence[str]) -> list[str]:
        """The command that talks the CLI's model-picker protocol."""
        raise NotImplementedError

    def model_efforts(self, item: dict) -> list[str]:
        """The model's own effort levels, in the CLI's order; empty when it reports none."""
        raise NotImplementedError


class CodexProvider(Provider):
    name = "codex"
    label = "Codex"
    login_command = "codex login"
    status_args = ("login", "status")

    def build_command(self, job_dir, config, preview_paths=()):
        args = [*resolve_command(config.provider, config.command), "-a", "never", "exec", "--ignore-user-config", "--ephemeral", "--skip-git-repo-check",
                "--sandbox", "read-only", *model_args(config), "--color", "never", "--json",
                "--cd", wsl.cli_path(job_dir, config), "--output-schema", wsl.cli_path(job_dir / "schema.json", config),
                "--output-last-message", wsl.cli_path(job_dir / "plan.json", config)]
        for feature in ("shell_tool", "unified_exec", "apps", "plugins", "remote_plugin", "hooks", "multi_agent", "browser_use", "computer_use", "image_generation", "skill_search", "sleep_tool"):
            args.extend(["--disable", feature])
        for key, value in {"web_search": "disabled", "model_reasoning_effort": config.reasoning_effort, "project_doc_max_bytes": 0, "model_instructions_file": wsl.cli_path(job_dir / "instructions.md", config)}.items():
            args.extend(["-c", key + "=" + json.dumps(value)])
        server = server_command(job_dir, config)
        for key, value in {**server, "required": True, "enabled_tools": TOOL_NAMES}.items():
            args.extend(["-c", "mcp_servers.worldprompt." + key + "=" + json.dumps(value)])
        for path in preview_paths:
            args.extend(["--image", wsl.cli_path(path, config)])
        if config.fast_mode:
            args.extend(["-c", 'service_tier="fast"'])
        return [*args, "-"]

    def prepare_input(self, job_dir, request, preview_paths):
        return job_dir / "request.json"  # Written by the driver; images travel as --image arguments.

    def read_result(self, events_path, config):
        usage = {}
        for line in events_path.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if isinstance(event, dict) and event.get("type") == "turn.completed" and isinstance(event.get("usage"), dict):
                usage = {k: v for k, v in event["usage"].items() if type(v) is int}
        return None, usage

    def login_status(self, completed):
        ready = completed.returncode == 0
        if ready and "api key" in (completed.stdout + completed.stderr).lower():
            return False, "Codex CLI is using an API key. Sign in with ChatGPT; APIs are not supported yet."
        return ready, None

    def model_list_command(self, command):
        return [*command, "-c", 'mcp_servers={}', "-c", 'features.hooks=false',
                "-c", 'features.plugins=false', "app-server"]

    def model_efforts(self, item):
        levels = item.get("supportedReasoningEfforts")
        levels = [level.get("reasoningEffort") for level in levels if isinstance(level, dict)] if isinstance(levels, list) else []
        return _valid_efforts(levels)


class ClaudeProvider(Provider):
    name = "claude"
    label = "Claude"
    login_command = "claude auth login"
    status_args = ("auth", "status")
    keeps_stdin_open = True  # The streaming protocol keeps stdin open until its result.
    extra_environment = {"CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1"}
    model_key = "value"

    def build_command(self, job_dir, config, preview_paths=()):
        # --bare deliberately is NOT used: it disables OAuth/keychain sign-in.
        # --safe-mode disables even explicitly supplied MCP servers. Restricted
        # mode retains subscription login and our strict MCP configuration.
        # No built-in action tools; only our private offline MCP server.
        mcp = {"mcpServers": {"worldprompt": server_command(job_dir, config)}}
        return [*resolve_command(config.provider, config.command), "--print", "--restricted", "--setting-sources", "",
                "--settings", json.dumps({"fastMode": config.fast_mode, "disableAllHooks": True, "autoMemoryEnabled": False, "claudeMdExcludes": ["**"]}),
                "--input-format", "stream-json", "--output-format", "stream-json", "--verbose",
                "--no-session-persistence", *model_args(config), "--effort", config.reasoning_effort,
                "--tools", "", "--strict-mcp-config", "--mcp-config", json.dumps(mcp),
                "--disable-slash-commands", "--no-chrome", "--permission-mode", "dontAsk",
                "--allowedTools", *["mcp__worldprompt__" + name for name in TOOL_NAMES],
                "--max-turns", str(MAX_CLAUDE_TURNS),
                "--system-prompt-file", wsl.cli_path(job_dir / "instructions.md", config),
                "--json-schema", json.dumps(plan_schema(config.max_operations), separators=(",", ":"))]

    def prepare_input(self, job_dir, request, preview_paths):
        from PIL import Image, UnidentifiedImageError
        content = [{"type": "text", "text": request}]
        media_types = {"PNG": "image/png", "JPEG": "image/jpeg", "GIF": "image/gif", "WEBP": "image/webp"}
        for path in preview_paths:
            path = Path(path)
            try:
                with Image.open(path) as picture:
                    media_type = media_types.get(picture.format)
                    picture.verify()
                if not media_type:
                    raise AgentError("Claude preview images must be PNG, JPEG, GIF, or WebP")
            except (OSError, UnidentifiedImageError, ValueError) as exc:
                raise AgentError("Claude preview image could not be decoded") from exc
            content.append({"type": "image", "source": {"type": "base64", "media_type": media_type,
                                                         "data": base64.b64encode(path.read_bytes()).decode("ascii")}})
        message = {"type": "user", "message": {"role": "user", "content": content}, "parent_tool_use_id": None}
        path = job_dir / "input.jsonl"
        path.write_text(json.dumps(message, ensure_ascii=False, separators=(",", ":")) + "\n", encoding="utf-8")
        return path

    def is_final_event(self, event):
        return isinstance(event, dict) and event.get("type") == "result"

    def read_result(self, events_path, config):
        result = None
        for line in events_path.read_text(encoding="utf-8-sig", errors="replace").splitlines():
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if self.is_final_event(event):
                result = event
        if result is None:
            raise AgentError("Claude returned no structured result. Check CLI compatibility and the private job logs.")
        if result.get("is_error") or result.get("subtype") != "success":
            subtype = result.get("subtype")
            if subtype == "error_max_turns":
                raise AgentError("Claude reached its bounded turn limit before returning a complete plan.")
            detail = self.failure_detail(events_path.parent / "stderr.log", events_path)
            raise AgentError(f"Claude planning failed. {detail}")
        plan = result.get("structured_output")
        if not isinstance(plan, dict):
            raise AgentError("Claude returned no structured plan. Update Claude Code and check the configured model.")
        if len(json.dumps(plan, ensure_ascii=False).encode("utf-8")) > config.max_plan_bytes:
            raise AgentError("Claude exceeded the plan size limit")
        raw_usage = result.get("usage", {})
        usage = {key: value for key, value in raw_usage.items() if type(value) is int and value >= 0} if isinstance(raw_usage, dict) else {}
        # Claude's input_tokens excludes cache reads/writes, whereas Codex includes
        # its cached subset. Normalize this boundary so the shared ledger reports
        # total input once while retaining cache counters as explanatory subsets.
        if "input_tokens" in usage:
            usage["input_tokens"] += usage.get("cache_creation_input_tokens", 0) + usage.get("cache_read_input_tokens", 0)
        return plan, usage

    def login_status(self, completed):
        ready = completed.returncode == 0
        if ready:
            try:
                status = json.loads(completed.stdout)
                ready = isinstance(status, dict) and status.get("loggedIn") is True
                if ready and status.get("authMethod") != "claude.ai":
                    return False, "Claude CLI is using API authentication. Sign in with a Claude subscription; APIs are not supported yet."
            except (ValueError, TypeError):
                return False, "Claude CLI returned an incompatible login status. Update Claude Code and sign in again."
        return ready, None

    def model_list_command(self, command):
        return [*command, "--print", "--safe-mode", "--setting-sources", "",
                "--input-format", "stream-json", "--output-format", "stream-json", "--verbose",
                "--no-session-persistence", "--tools", "", "--strict-mcp-config", "--mcp-config",
                '{"mcpServers":{}}', "--disable-slash-commands", "--no-chrome"]

    def model_efforts(self, item):
        levels = item.get("supportedEffortLevels") if item.get("supportsEffort") is True else []
        return _valid_efforts(levels if isinstance(levels, list) else [])


def _valid_efforts(levels) -> list[str]:
    return list(dict.fromkeys(level for level in levels[:32] if is_reasoning_effort(level)))


PROVIDERS = {provider.name: provider for provider in (CodexProvider(), ClaudeProvider())}


def get_provider(name: str) -> Provider:
    """The provider for an ``AgentConfig.provider`` value (already validated there)."""
    return PROVIDERS[name]
