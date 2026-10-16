"""Portable application settings. CLI providers own account credentials."""
from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile

from . import wsl
from .agent_types import PROVIDER_NAMES, validate_model_settings
from .limits import MAX_OPERATIONS, SETTING_RANGES, UNLIMITED_JOB_SETTINGS


def data_dir() -> Path:
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA", "").strip()
        return (Path(base) if base else Path.home() / "AppData/Local") / "WorldPrompt"
    base = Path(os.environ.get("XDG_DATA_HOME", ""))
    if not base.is_absolute():
        base = Path.home() / ".local/share"
    return base / "worldprompt"


def config_path() -> Path:
    override = os.environ.get("WORLDPROMPT_CONFIG", "").strip()
    if override:
        return Path(override).expanduser()
    if os.name == "nt":
        return data_dir() / "config.json"
    base = Path(os.environ.get("XDG_CONFIG_HOME", ""))
    if not base.is_absolute():
        base = Path.home() / ".config"
    return base / "worldprompt/config.json"


def gui_access_path(path=None) -> Path:
    """Stable private file used to reopen the running settings GUI."""
    target = Path(path) if path else config_path()
    return target.with_name(target.name + ".gui-access.json")


def connection_path() -> Path:
    override = os.environ.get("WORLDPROMPT_CONNECTION_FILE", "").strip()
    return Path(override).expanduser() if override else data_dir() / "connection.json"


def default_config() -> dict:
    return {"provider": "codex", "codex_command": [],
            "claude_command": [], "codex_model": "", "claude_model": "",
            "codex_reasoning_effort": "medium", "claude_reasoning_effort": "medium",
            "codex_fast_mode": False, "claude_fast_mode": False,
            "jobs_dir": str(data_dir() / "jobs"), "job_limits": False, "timeout_seconds": 3600,
            "high_timeout_seconds": 3600, "max_operations": MAX_OPERATIONS, "port": 8765, "world_port": 8768,
            "world_concurrency": 2, "world_max_pending": 32, "world_lease_seconds": 120,
            "world_max_jobs_per_player": 4,
            "world_token_file": "", "minecraft_dir": ""}


def job_limits(config: dict) -> dict:
    """The job limits in force: the saved values when job_limits is on, otherwise unreachable ones."""
    config = {**default_config(), **config}
    if config["job_limits"]:
        return {key: config[key] for key in UNLIMITED_JOB_SETTINGS}
    return dict(UNLIMITED_JOB_SETTINGS)


def normalize_folder(value: str) -> str:
    value = str(value).strip()
    if not value:
        return ""
    if wsl.is_wsl() and len(value) >= 3 and value[1] == ":" and value[2] in ("/", chr(92)):
        value = wsl.wslpath("-u", value)
    return str(Path(value).expanduser().resolve())


def validate_command(command) -> list[str]:
    if not isinstance(command, list) or len(command) > 16 or any(
            not isinstance(s, str) or not s.strip() or "\x00" in s or len(s) > 4096 for s in command):
        raise ValueError("Command must be a JSON array of arguments, or empty for automatic discovery.")
    return list(command)


def validate_config(value: dict) -> dict:
    if not isinstance(value, dict):
        raise ValueError("Settings must be a JSON object.")
    defaults = default_config()
    unknown = set(value) - set(defaults) - {"world_jobs_dir"}
    if unknown:
        raise ValueError("Unknown settings: " + ", ".join(sorted(unknown)))
    config = dict(defaults, **value)
    if not isinstance(config["provider"], str) or config["provider"] not in PROVIDER_NAMES:
        raise ValueError("Only Codex CLI and Claude CLI are supported. API providers are not supported yet.")
    for provider in PROVIDER_NAMES:
        config[provider + "_command"] = validate_command(config[provider + "_command"])
        model = config[provider + "_model"]  # Empty: the CLI's own default model.
        if not isinstance(model, str) or len(model) > 128:
            raise ValueError(f"Enter a model for {provider}, or leave it empty for the CLI default.")
        config[provider + "_model"] = model.strip()
        validate_model_settings(provider, model.strip(), config[provider + "_reasoning_effort"],
                                config[provider + "_fast_mode"])
    if type(config["job_limits"]) is not bool:
        raise ValueError("job_limits must be true or false.")
    if type(config["world_max_jobs_per_player"]) is int and config["world_max_jobs_per_player"] == 0:
        # Settings saved before job_limits used 0 for no per-player limit.
        config["world_max_jobs_per_player"] = defaults["world_max_jobs_per_player"]
    for key, (low, high) in SETTING_RANGES.items():
        if type(config[key]) is not int or config[key] < low or high is not None and config[key] > high:
            raise ValueError(f"{key} must be an integer from {low} to {high}." if high is not None
                             else f"{key} must be an integer of at least {low}.")
    if config["port"] == config["world_port"]:
        raise ValueError("The settings and Minecraft ports must be different.")
    for key in ("jobs_dir", "world_token_file", "minecraft_dir", "world_jobs_dir"):
        if key not in config:
            continue
        if not isinstance(config[key], str) or "\x00" in config[key] or len(config[key]) > 4096:
            raise ValueError(f"Invalid {key} path.")
        config[key] = normalize_folder(config[key])
    if "world_jobs_dir" in config and not config["world_jobs_dir"]:
        raise ValueError("Custom world job storage cannot be empty.")
    if not config["jobs_dir"]:
        raise ValueError("Choose a job storage directory.")
    return config


def load_config(path=None) -> dict:
    target = Path(path) if path else config_path()
    if not target.exists():
        return default_config()
    return validate_config(json.loads(target.read_text(encoding="utf-8-sig")))


def write_private_json(target: Path, data, *, indent=2, ensure_ascii=True, create_parent=True) -> None:
    """Atomically replace ``target`` with mode-0600 JSON, flushed to disk before the rename."""
    target = Path(target)
    if create_parent:
        target.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=target.parent, delete=False) as handle:
            temporary = Path(handle.name)
            json.dump(data, handle, indent=indent, ensure_ascii=ensure_ascii)
            if indent is not None:
                handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, target)
    finally:
        if temporary:
            temporary.unlink(missing_ok=True)


def save_config(config: dict, path=None) -> None:
    write_private_json(Path(path) if path else config_path(), validate_config(config))
