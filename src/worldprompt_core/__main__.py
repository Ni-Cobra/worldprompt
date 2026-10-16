"""WorldPrompt Core desktop launcher and headless service entry point."""
from __future__ import annotations

import argparse
import multiprocessing
import os
import sys


def reopen_running_core(config, config_file=None, port=None, open_browser=True):
    """A second desktop launch opens the existing instance's settings."""
    import json
    from pathlib import Path
    from urllib.request import ProxyHandler, build_opener
    import webbrowser
    from .config import config_path, gui_access_path
    try:
        from urllib.request import Request
        access = json.loads(gui_access_path(config_file).read_text(encoding="utf-8"))
        if not isinstance(access, dict):
            return False
        session = access.get("session")
        if not isinstance(session, str) or not session or not session.isascii() or any(
                not character.isalnum() and character not in "_-" for character in session):
            return False
        running_port = access.get("port", config["port"] if port is None else port)
        if type(running_port) is not int or not 1 <= running_port <= 65535:
            return False
        url = f"http://127.0.0.1:{running_port}"
        request = Request(url + "/api/state", headers={"X-WorldPrompt-Session": session})
        with build_opener(ProxyHandler({})).open(request, timeout=2) as response:
            state = json.loads(response.read(1024 * 1024))
        if (state.get("product") != "WorldPrompt Core"
                or Path(state.get("config_path", "")).resolve() != Path(config_file or config_path()).resolve()
                or state.get("service", {}).get("stopping", True)):
            return False
    except (OSError, ValueError, TypeError, AttributeError):
        return False
    if open_browser:
        webbrowser.open(url + "/#session=" + session)
    return True


def main(argv=None):
    windowless = os.name == "nt" and sys.stderr is None
    if windowless:
        from .config import data_dir
        logs = data_dir() / "logs"
        logs.mkdir(parents=True, exist_ok=True, mode=0o700)
        sys.stdout = sys.stderr = (logs / "core.log").open("a", encoding="utf-8", buffering=1)
    from . import __version__
    from .config import load_config
    parser = argparse.ArgumentParser(description="WorldPrompt Core — Minecraft planning with Codex or Claude CLI.")
    parser.add_argument("--version", action="version", version=f"WorldPrompt Core {__version__}")
    parser.add_argument("--config", help="Application settings JSON path")
    sub = parser.add_subparsers(dest="command")
    serve = sub.add_parser("serve", help="Run Core and its local settings GUI")
    serve.add_argument("--no-browser", action="store_true", help="Run without opening the settings window")
    serve.add_argument("--no-tray", action="store_true", help="Disable the Windows tray icon (for services)")
    serve.add_argument("--port", type=int, help="Override settings GUI port for this launch")
    tools = sub.add_parser("planner-tools", help="Private stdio planner validation tools")
    tools.add_argument("--request", required=True)
    args = parser.parse_args(argv)
    if args.command == "planner-tools":
        from .planner_tools import serve as serve_tools
        return serve_tools(args.request)
    try:
        from .server import serve as launch
        config = load_config(args.config)
        if os.name == "nt" and reopen_running_core(
                config, args.config, getattr(args, "port", None),
                not getattr(args, "no_browser", False)):
            return 0
        launch(config, port=getattr(args, "port", None), config_path=args.config,
               open_browser=not getattr(args, "no_browser", False),
               tray_enabled=not getattr(args, "no_tray", False))
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"WorldPrompt Core: {exc}", file=sys.stderr)
        if windowless:
            import ctypes
            ctypes.windll.user32.MessageBoxW(None, str(exc), "WorldPrompt Core could not start", 0x10)
        return 1
    return 0


if __name__ == "__main__":
    multiprocessing.freeze_support()
    raise SystemExit(main())
