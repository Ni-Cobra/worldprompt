"""Portable settings, supported providers and atomic configuration persistence."""
import json
import os
from pathlib import Path

import pytest

from worldprompt_core import config


def test_empty_commands_default_to_automatic_discovery_and_round_trip(tmp_path):
    from worldprompt_core.pipeline import agent_config_for
    defaults = config.default_config()
    assert defaults['codex_command'] == defaults['claude_command'] == []
    config.save_config(defaults, tmp_path / 'config.json')
    saved = config.load_config(tmp_path / 'config.json')
    for provider in ('codex', 'claude'):
        assert agent_config_for(dict(saved, provider=provider)).command == ()


@pytest.mark.parametrize("values", [
    {"provider": "openai"}, {"provider": "anthropic"}, {"provider": "local"},
    {"provider": []}, {"provider": {}},
    {"api_key": "fixture-key"}, {"anthropic_api_key": "fixture-key"},
    {"codex_command": "codex"}, {"claude_command": [""]}, {"claude_command": ["claude", "\x00"]},
    {"world_concurrency": True}, {"world_port": 8765}, {"timeout_seconds": 0}, {"world_jobs_dir": ""},
    {"codex_reasoning_effort": ""}, {"claude_reasoning_effort": "High"},
    {"claude_reasoning_effort": "x" * 33}, {"codex_reasoning_effort": "high\" --x"},
    {"codex_reasoning_effort": []}, {"codex_fast_mode": "true"}, {"claude_fast_mode": 1},
    {"claude_fast_mode": True, "claude_model": "sonnet"},
    {"max_operations": 0}, {"max_operations": 513}, {"max_operations": True}, {"max_operations": "64"},
])
def test_invalid_or_unsupported_settings_are_rejected(values):
    with pytest.raises(ValueError):
        config.validate_config(values)


def test_settings_round_trip_keeps_both_cli_profiles(tmp_path):
    target = tmp_path / "settings.json"
    values = config.validate_config({"provider": "claude", "codex_command": ["codex-fixture"],
                                     "claude_command": ["claude-fixture"], "claude_model": "sonnet",
                                     "codex_fast_mode": True, "codex_reasoning_effort": "xhigh",
                                     "claude_reasoning_effort": "xhigh",
                                     "jobs_dir": str(tmp_path / "jobs")})
    config.save_config(values, target)
    assert config.load_config(target) == values
    assert json.loads(target.read_text())["provider"] == "claude"
    if os.name == "posix":
        assert target.stat().st_mode & 0o777 == 0o600


def test_windows_utf8_bom_settings_load(tmp_path):
    target = tmp_path / "config.json"
    target.write_text(json.dumps({"provider": "claude"}), encoding="utf-8-sig")
    assert config.load_config(target)["provider"] == "claude"


def test_failed_atomic_write_preserves_previous_settings(tmp_path, monkeypatch):
    target = tmp_path / "config.json"
    original = b'{"sentinel": "previous content"}\n'
    target.write_bytes(original)
    def fail_replace(*args):
        raise OSError("Fixture full storage")
    monkeypatch.setattr(config.os, "replace", fail_replace)
    with pytest.raises(OSError):
        config.write_private_json(target, {"sentinel": "replacement"})
    assert target.read_bytes() == original
    assert list(tmp_path.iterdir()) == [target]


@pytest.mark.skipif(os.name == "nt", reason="Linux XDG directory convention")
@pytest.mark.parametrize("invalid", ["", "relative-directory"])
def test_invalid_xdg_directories_fall_back_to_home(tmp_path, monkeypatch, invalid):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setenv("XDG_DATA_HOME", invalid)
    monkeypatch.setenv("XDG_CONFIG_HOME", invalid)
    monkeypatch.delenv("WORLDPROMPT_CONFIG", raising=False)
    monkeypatch.delenv("WORLDPROMPT_CONNECTION_FILE", raising=False)
    assert config.data_dir() == tmp_path / ".local/share/worldprompt"
    assert config.config_path() == tmp_path / ".config/worldprompt/config.json"
    assert config.connection_path() == config.data_dir() / "connection.json"


def test_blank_connection_override_uses_discovery_default(monkeypatch):
    monkeypatch.setenv("WORLDPROMPT_CONNECTION_FILE", "")
    assert config.connection_path() == config.data_dir() / "connection.json"


@pytest.mark.skipif(os.name != "nt", reason="Native Windows discovery directory convention")
def test_blank_windows_localappdata_falls_back_to_home(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setenv("LOCALAPPDATA", "")
    assert config.data_dir() == tmp_path / "AppData/Local/WorldPrompt"


def test_operation_limit_applies_to_every_pass():
    from worldprompt_core.pipeline import agent_config_for
    assert config.default_config()["max_operations"] == 512
    values = config.validate_config({"job_limits": True, "max_operations": 64})
    assert agent_config_for(values).max_operations == 64
    high = agent_config_for(values, "high")
    assert high.max_operations == 64 and high.timeout_seconds == 3600 and high.max_plan_bytes == 262_144


def test_job_limits_are_off_by_default_and_unreachable():
    from worldprompt_core.limits import UNLIMITED_JOB_SETTINGS
    from worldprompt_core.pipeline import agent_config_for
    defaults = config.default_config()
    assert defaults["job_limits"] is False
    assert config.job_limits(defaults) == config.job_limits({}) == UNLIMITED_JOB_SETTINGS
    # The stored values are kept for when the limits are switched on.
    assert defaults["timeout_seconds"] == defaults["high_timeout_seconds"] == 3600
    assert agent_config_for(defaults).timeout_seconds == agent_config_for({}, "high").timeout_seconds == 365 * 86_400
    assert agent_config_for(defaults).max_operations == 512
    on = config.validate_config({"job_limits": True, "timeout_seconds": 60, "world_max_jobs_per_player": 2})
    assert config.job_limits(on) == {"timeout_seconds": 60, "high_timeout_seconds": 3600, "max_operations": 512,
                                     "world_concurrency": 2, "world_max_pending": 32, "world_max_jobs_per_player": 2}
    assert agent_config_for(on).timeout_seconds == 60 and agent_config_for(on, "high").timeout_seconds == 3600


@pytest.mark.parametrize("value", [1, "true", None])
def test_job_limits_must_be_a_boolean(value):
    with pytest.raises(ValueError, match="job_limits"):
        config.validate_config({"job_limits": value})


@pytest.mark.parametrize("value", [1, 256])
def test_per_player_job_limit_accepts_one_to_256(value):
    assert config.validate_config({"world_max_jobs_per_player": value})["world_max_jobs_per_player"] == value


def test_legacy_zero_per_player_limit_loads_as_the_default():
    assert config.validate_config({"world_max_jobs_per_player": 0})["world_max_jobs_per_player"] == 4


@pytest.mark.parametrize("value", [-1, 257, True, False, 1.5, "2", None])
def test_per_player_job_limit_rejects_invalid_values(value):
    with pytest.raises(ValueError, match="world_max_jobs_per_player"):
        config.validate_config({"world_max_jobs_per_player": value})


@pytest.mark.parametrize("key", ["timeout_seconds", "high_timeout_seconds"])
def test_timeouts_have_a_minimum_but_no_maximum(key):
    assert config.validate_config({key: 1})[key] == 1
    assert config.validate_config({key: 10_000_000})[key] == 10_000_000
    for value in (0, 1.5, True):
        with pytest.raises(ValueError, match=key):
            config.validate_config({key: value})


@pytest.mark.parametrize("wsl", [True, False])
def test_windows_drive_folders_are_translated_only_on_wsl(monkeypatch, tmp_path, wsl):
    from unittest.mock import patch
    import subprocess
    from worldprompt_core import wsl as wsl_module
    done = subprocess.CompletedProcess([], 0, str(tmp_path) + "\n", "")
    monkeypatch.setattr(wsl_module, "is_wsl", lambda: wsl)
    with patch.object(wsl_module.subprocess, "run", return_value=done) as run:
        result = config.normalize_folder("C:\\Users\\me\\mc")
    if wsl:
        assert run.call_args.args[0][:2] == ["wslpath", "-u"]
        assert result == str(tmp_path.resolve())
    else:
        run.assert_not_called()  # No wslpath on a plain host; the folder simply will not exist.
