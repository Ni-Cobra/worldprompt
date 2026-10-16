"""WSL interop: detection, path translation and the wsl.exe launch wrapper."""
import os
import subprocess
from unittest.mock import patch

import pytest

from worldprompt_core import wsl
from worldprompt_core.agent_types import AgentConfig, AgentError


@pytest.mark.parametrize("text, environment, expected", [
    ("6.6.87.2-microsoft-standard-WSL2\n", {}, True), ("5.15.0-MICROSOFT\n", {}, True),
    ("6.1.0-17-generic\n", {}, False), ("6.1.0-17-generic\n", {"WSL_DISTRO_NAME": "Ubuntu"}, True),
    ("6.6.0-microsoft\n", {"WSL_DISTRO_NAME": ""}, True)])
def test_is_wsl_uses_environment_or_kernel_release(tmp_path, text, environment, expected):
    release = tmp_path / "osrelease"
    release.write_text(text)
    env = {key: value for key, value in os.environ.items() if key != "WSL_DISTRO_NAME"}
    with patch.dict(os.environ, env, clear=True), patch.dict(os.environ, environment), \
            patch.object(wsl, "_OSRELEASE", release), patch.object(wsl.os, "name", "posix"):
        assert wsl.is_wsl() is expected


def test_unreadable_kernel_release_is_not_wsl(tmp_path):
    with patch.dict(os.environ, {}, clear=True), patch.object(wsl, "_OSRELEASE", tmp_path / "missing"):
        assert not wsl.is_wsl()


def test_windows_cli_is_an_exe_driven_from_posix():
    with patch.object(wsl.os, "name", "posix"):
        assert wsl.windows_cli(AgentConfig(command=("/mnt/c/codex.EXE",)))
        assert not wsl.windows_cli(AgentConfig(command=("/usr/bin/codex",)))
        assert not wsl.windows_cli(AgentConfig())
    with patch.object(wsl.os, "name", "nt"):
        assert not wsl.windows_cli(AgentConfig(command=("C:/codex.exe",)))


def test_cli_path_is_posix_for_a_native_cli_and_translated_for_a_windows_cli(tmp_path):
    assert wsl.cli_path(tmp_path / "plan.json", AgentConfig()) == str((tmp_path / "plan.json").resolve())
    config = AgentConfig(command=("/mnt/c/codex.exe",))
    done = subprocess.CompletedProcess([], 0, "C:\\jobs\\plan.json\n", "")
    with patch.object(wsl.os, "name", "posix"), patch.object(wsl.subprocess, "run", return_value=done) as run:
        assert wsl.cli_path(tmp_path / "plan.json", config) == "C:\\jobs\\plan.json"
    assert run.call_args.args[0][:2] == ["wslpath", "-w"]
    with patch.object(wsl.os, "name", "posix"), patch.object(wsl.subprocess, "run", side_effect=FileNotFoundError):
        with pytest.raises(AgentError, match="wslpath"):
            wsl.cli_path(tmp_path / "plan.json", config)


def test_wsl_exec_needs_the_distribution_name():
    with patch.dict(os.environ, {"WSL_DISTRO_NAME": "Ubuntu"}):
        assert wsl.wsl_exec(["python", "-c", "1"]) == ["wsl.exe", "--distribution", "Ubuntu", "--exec", "python", "-c", "1"]
    env = {key: value for key, value in os.environ.items() if key != "WSL_DISTRO_NAME"}
    with patch.dict(os.environ, env, clear=True), pytest.raises(AgentError, match="WSL_DISTRO_NAME"):
        wsl.wsl_exec(["python"])
