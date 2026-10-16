"""Resolve automatic CLI commands using PATH, installed apps, then editor bundles."""
from __future__ import annotations

import os
from pathlib import Path
import platform
import re
import shutil
import sys
from typing import Sequence


def _files(directory: Path, pattern: str) -> list[Path]:
    try:
        return [path for path in directory.glob(pattern) if path.is_file()]
    except OSError:
        return []


def _version(value: str) -> tuple:
    # Version folders/extensions sort numerically (2.1.10 follows 2.1.9).
    return tuple(int(part) for part in re.findall(r"\d+", value)), value


def _modified(path: Path) -> tuple:
    try:
        return path.stat().st_mtime_ns, str(path)
    except OSError:
        return 0, str(path)


def installed_commands(provider: str, *, home: Path | None = None,
                       system: str | None = None) -> list[Path]:
    """Inspect bounded, known CLI locations without launching desktop apps."""
    home = Path.home() if home is None else home
    system = sys.platform if system is None else system
    windows = system == "win32"
    executable = provider + (".exe" if windows else "")
    candidates = [home / ".local/bin" / executable]
    if windows:
        local = Path(os.environ.get("LOCALAPPDATA") or home / "AppData/Local")
        roaming = Path(os.environ.get("APPDATA") or home / "AppData/Roaming")
        if provider == "codex":
            # Codex app directories are build hashes, not sortable versions.
            candidates += sorted(_files(local / "OpenAI/Codex/bin", "*/codex.exe"),
                                 key=_modified, reverse=True)
        elif provider == "claude":
            desktop = _files(roaming / "Claude/claude-code", "*/claude.exe")
            desktop += _files(local / "Packages",
                              "Claude_*/LocalCache/Roaming/Claude/claude-code/*/claude.exe")
            candidates += sorted(desktop, key=lambda path: _version(path.parent.name), reverse=True)
    package = "anthropic.claude-code" if provider == "claude" else "openai.chatgpt"
    host = "windows" if windows else "darwin" if system == "darwin" else "linux"
    architecture = "aarch64" if platform.machine().lower() in {"arm64", "aarch64"} else "x86_64"
    relative = ("resources/native-binary/" + executable if provider == "claude" else
                f"bin/{host}-{architecture}/{executable}")
    extensions = []
    for directory in (".vscode", ".vscode-server", ".vscode-insiders", ".vscode-server-insiders"):
        extensions += _files(home / directory / "extensions", package + "-*/" + relative)
    candidates += sorted(extensions, key=lambda path: _version(next(
        part for part in path.parts if part.startswith(package + "-"))), reverse=True)
    return candidates


def discover_executable(provider: str) -> str:
    found = shutil.which(provider)
    if found:
        return str(Path(found).absolute())
    for candidate in installed_commands(provider):
        try:
            if candidate.is_file() and (os.name == "nt" or os.access(candidate, os.X_OK)):
                return str(candidate)
        except OSError:
            continue
    return provider


def resolve_command(provider: str, command: Sequence[str]) -> list[str]:
    """An empty command selects discovery; explicit arguments remain untouched.

    Explicit paths, wrappers and trailing arguments remain user-controlled.
    Do not replace a broken custom path with a different installation.
    """
    if not command:
        return [discover_executable(provider)]
    return list(command)
