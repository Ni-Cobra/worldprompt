"""Default commands find installed CLIs without overwriting user commands."""
import os
from pathlib import Path

import pytest

from worldprompt_core import cli_discovery as discovery


def executable(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch()
    path.chmod(0o700)
    return path


@pytest.fixture
def installation(tmp_path, monkeypatch):
    monkeypatch.setattr(discovery.shutil, "which", lambda name: None)
    monkeypatch.setattr(discovery.Path, "home", lambda: tmp_path)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    monkeypatch.setenv("APPDATA", str(tmp_path / "roaming"))
    return tmp_path


def test_automatic_path_wins_over_app_search(installation, monkeypatch):
    path = executable(installation / "path/claude")
    monkeypatch.setattr(discovery.shutil, "which", lambda name: str(path))
    monkeypatch.setattr(discovery, "installed_commands", lambda provider: pytest.fail("PATH already resolved"))
    assert discovery.resolve_command("claude", []) == [str(path)]


def test_saved_default_rediscovers_installation_after_update(installation, monkeypatch):
    old = executable(installation / "old/codex")
    new = installation / "new/codex"
    monkeypatch.setattr(discovery, "installed_commands", lambda provider: [new, old])
    saved = []
    assert discovery.resolve_command("codex", saved) == [str(old)]
    executable(new)
    old.unlink()
    assert discovery.resolve_command("codex", saved) == [str(new)]
    assert saved == []


@pytest.mark.parametrize("command", [["/missing/claude", "--verbose"],
                                    [r"C:\missing\codex.exe"], ["wrapper", "claude"], ["./codex"], ["codex"], ["claude"]])
def test_explicit_commands_remain_unchanged(command, monkeypatch):
    monkeypatch.setattr(discovery, "discover_executable", lambda provider: pytest.fail("Explicit command"))
    assert discovery.resolve_command("codex", command) == command


@pytest.mark.parametrize("provider", ["codex", "claude"])
def test_missing_cli_preserves_default_error(installation, provider):
    assert discovery.resolve_command(provider, []) == [provider]


def test_claude_desktop_cache_precedes_extension_and_orders_versions(installation):
    older = executable(installation / "roaming/Claude/claude-code/2.1.9/claude.exe")
    desktop = executable(installation / "local/Packages/Claude_test/LocalCache/Roaming/Claude/claude-code/2.1.10/claude.exe")
    extension = executable(installation / ".vscode/extensions/anthropic.claude-code-2.1.999-win32-x64/resources/native-binary/claude.exe")
    gui = executable(installation / "local/Claude/claude.exe")
    paths = discovery.installed_commands("claude", system="win32")
    assert paths.index(desktop) < paths.index(older) < paths.index(extension)
    assert gui not in paths
    assert paths[0] == installation / ".local/bin/claude.exe"


def test_codex_app_build_hashes_use_modification_time(installation):
    old = executable(installation / "local/OpenAI/Codex/bin/ffff/codex.exe")
    new = executable(installation / "local/OpenAI/Codex/bin/aaaa/codex.exe")
    os.utime(old, (1, 1))
    os.utime(new, (2, 2))
    paths = discovery.installed_commands("codex", system="win32")
    assert paths.index(new) < paths.index(old)


@pytest.mark.parametrize("provider", ["claude", "codex"])
def test_linux_native_and_remote_extension_locations(installation, monkeypatch, provider):
    monkeypatch.setattr(discovery.platform, "machine", lambda: "x86_64")
    relative = ("anthropic.claude-code-2.1.10-linux-x64/resources/native-binary/claude"
                if provider == "claude" else "openai.chatgpt-26.9.10-linux-x64/bin/linux-x86_64/codex")
    bundled = executable(installation / ".vscode-server/extensions" / relative)
    native = executable(installation / ".local/bin" / provider)
    paths = discovery.installed_commands(provider, system="linux")
    assert paths.index(native) < paths.index(bundled)


def test_inaccessible_app_directory_is_ignored(monkeypatch, tmp_path):
    def denied(*args):
        raise PermissionError("denied")
    monkeypatch.setattr(Path, "glob", denied)
    assert discovery._files(tmp_path, "*/claude.exe") == []


def test_codex_windows_vscode_fallback_without_desktop_app(installation, monkeypatch):
    monkeypatch.setattr(discovery.platform, "machine", lambda: "AMD64")
    old = executable(installation / ".vscode/extensions/openai.chatgpt-26.9.9-win32-x64/bin/windows-x86_64/codex.exe")
    new = executable(installation / ".vscode/extensions/openai.chatgpt-26.9.10-win32-x64/bin/windows-x86_64/codex.exe")
    candidates = discovery.installed_commands("codex", system="win32")
    monkeypatch.setattr(discovery, "installed_commands", lambda provider: candidates)
    assert discovery.resolve_command("codex", []) == [str(new)]
    new.unlink()
    assert discovery.resolve_command("codex", []) == [str(old)]
