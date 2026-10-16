"""Windows Subsystem for Linux interop: detection, path translation and Windows-CLI launching.

Core on WSL may drive a Windows ``.exe`` CLI, and Minecraft pairing may need Windows
paths for a Windows-side instance. Everything that shells out to ``wslpath`` or builds
a ``wsl.exe`` command lives here.
"""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
from typing import Sequence

from .agent_types import AgentConfig, AgentError

_OSRELEASE = Path("/proc/sys/kernel/osrelease")


def is_wsl() -> bool:
    """True inside Windows Subsystem for Linux (the one place /mnt/<drive> means a Windows drive)."""
    if os.name == "nt":
        return False
    if os.environ.get("WSL_DISTRO_NAME"):
        return True
    try:
        return "microsoft" in _OSRELEASE.read_text(encoding="ascii", errors="replace").lower()
    except OSError:
        return False


def wslpath(flag: str, path: str) -> str:
    """Translate a path with ``wslpath`` (``-u`` to Linux, ``-w`` to Windows); raises on failure."""
    return subprocess.run(["wslpath", flag, path], capture_output=True, text=True, check=True, timeout=10).stdout.strip()


def windows_cli(config: AgentConfig) -> bool:
    """True when the configured CLI is a Windows ``.exe`` driven from a POSIX host (WSL)."""
    return os.name != "nt" and bool(config.command) and config.command[0].lower().endswith(".exe")


def cli_path(path: Path, config: AgentConfig) -> str:
    """The path as the configured CLI must see it: Windows form for a Windows CLI, else absolute POSIX."""
    if windows_cli(config):
        try:
            return wslpath("-w", str(path.resolve()))
        except (OSError, subprocess.SubprocessError) as exc:
            raise AgentError("Windows CLI execution requires WSL path translation (wslpath)") from exc
    return str(path.resolve())


def wsl_exec(command: Sequence[str]) -> list[str]:
    """Wrap a Linux command so a Windows process can run it inside this WSL distribution."""
    distro = os.environ.get("WSL_DISTRO_NAME")
    if not distro:
        raise AgentError("Windows CLI planner tools require WSL_DISTRO_NAME")
    return ["wsl.exe", "--distribution", distro, "--exec", *command]
