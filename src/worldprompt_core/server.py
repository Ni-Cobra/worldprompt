"""Local WorldPrompt Core settings UI and Minecraft service."""
from __future__ import annotations

import copy
import hmac
import json
import os
from pathlib import Path, PurePosixPath
import secrets
import signal
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse
import webbrowser

from . import __version__, wsl
from .agent_types import PROVIDER_NAMES
from .config import (config_path as default_config_path, connection_path, gui_access_path, job_limits, save_config,
                     validate_config, validate_command, write_private_json, normalize_folder)
from .limits import TERMINAL_STATUSES
from .pipeline import agent_config_for
from .http_utils import discard_rejected_body

STATIC_DIR = Path(__file__).with_name("static")
# The only files the GUI serves; anything else under static/ (such as font licenses) stays private.
STATIC_FILES = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/style.css": ("style.css", "text/css; charset=utf-8"),
    "/icon.svg": ("icon.svg", "image/svg+xml"),
    "/icon.png": ("icon.png", "image/png"),
    **{f"/fonts/{name}.woff2": (f"fonts/{name}.woff2", "font/woff2") for name in (
        "newsreader-latin", "public-sans-latin", "public-sans-latin-ext",
        "jetbrains-mono-latin", "jetbrains-mono-latin-ext")},
}
MAX_CONFIG_BACKUPS = 3
RESTART_FIELDS = {"port", "world_port", "jobs_dir", "world_jobs_dir", "world_token_file"}


class CoreApp:
    def __init__(self, config, supervisor, token_path, world_url, config_path=None):
        self.config = copy.deepcopy(config)
        self.running_config = copy.deepcopy(config)
        self.supervisor = supervisor
        self.config_path = Path(config_path) if config_path else default_config_path()
        self.token_path = Path(token_path)
        self.world_url = world_url
        self.lock = threading.RLock()
        self.session = secrets.token_urlsafe(32)
        self.providers = {}
        self.models = {}
        self.restart_required = False
        self.stopping = False
        self.shutdown_callback = None

    def snapshot(self):
        with self.lock, self.supervisor.lock:
            recent = sorted((job for job in self.supervisor.jobs.values() if job["status"] in TERMINAL_STATUSES),
                            key=lambda job: job["started_at"], reverse=True)
            cleanup_ids = [job["id"] for job in recent
                           if not self.supervisor.cleanup_blocked_reason(job)]
            return {"product": "WorldPrompt Core", "version": __version__,
                    "config": copy.deepcopy(self.config), "config_path": str(self.config_path),
                    "providers": copy.deepcopy(self.providers),
                    "models": copy.deepcopy(self.models),
                    "restart_required": self.restart_required,
                    "service": {"world_url": self.world_url, "connection_file": str(connection_path()),
                                "token_file": str(self.token_path), "stopping": self.stopping},
                    "jobs": self.supervisor.list_jobs(),
                    "recent_jobs": [dict(self.supervisor.public(job),
                                         cleanup_blocked_reason=self.supervisor.cleanup_blocked_reason(job))
                                    for job in recent[:8]],
                    "recent_job_count": len(recent), "cleanup_job_ids": cleanup_ids,
                    "provider_notice": "Only Codex CLI and Claude CLI are supported. APIs and other agents are not supported yet. WorldPrompt is an open-source project; you can fork it to add another provider."}

    def update_config(self, values):
        with self.lock, self.supervisor.lock:
            if self.stopping:
                raise ValueError("Core is stopping.")
            if self.supervisor.list_jobs():
                raise ValueError("Wait for active Minecraft jobs to finish before changing settings.")
            config = validate_config(values)
            save_config(config, self.config_path)
            for provider in PROVIDER_NAMES:
                if config[provider + "_command"] != self.config.get(provider + "_command"):
                    self.models.pop(provider, None)
            self.config = config
            self.providers.clear()
            self.restart_required = any(config.get(key) != self.running_config.get(key) for key in RESTART_FIELDS)
            effective = dict(config)
            for key in RESTART_FIELDS:
                if key in self.running_config:
                    effective[key] = self.running_config[key]
                else:
                    effective.pop(key, None)
            self.supervisor.config = copy.deepcopy(effective)
            limits = job_limits(config)
            self.supervisor.concurrency = limits["world_concurrency"]
            self.supervisor.max_pending = limits["world_max_pending"]
            self.supervisor.lease_seconds = config["world_lease_seconds"]
            self.supervisor.max_jobs_per_player = limits["world_max_jobs_per_player"]
        return self.snapshot()

    def refresh_models(self, provider, command=None):
        if not isinstance(provider, str) or provider not in PROVIDER_NAMES:
            raise ValueError("Choose Codex or Claude CLI.")
        with self.lock:
            if self.stopping:
                return
            values = dict(self.config, provider=provider)
            if command is not None:
                values[provider + "_command"] = validate_command(command)
            config = agent_config_for(values)
            requested = list(config.command)
            previous = self.models.get(provider, {})
            same_command = previous.get("command") == requested
            if previous.get("loading") and same_command:
                return
            entry = {"loading": True, "command": requested,
                     "items": previous.get("items", []) if same_command else [],
                     "message": "Loading models from CLI…"}
            self.models[provider] = entry
        def discover():
            from .models import list_models, ModelListError
            try:
                items = list_models(config)
                result = {"items": items, "message": "Models reported by your CLI. Access depends on your account."
                          if items else "The CLI returned no models. You can enter a custom model ID."}
            except (OSError, subprocess.SubprocessError, ModelListError):
                result = {"items": entry["items"], "error": True,
                          "message": "Could not refresh models. Check the CLI connection and try again, or enter a custom model ID."}
            except Exception:
                result = {"items": entry["items"], "error": True,
                          "message": "The CLI returned an incompatible model list. Update it or enter a custom model ID."}
            with self.lock:
                if self.models.get(provider) is entry:
                    self.models[provider] = dict(result, loading=False, command=requested)
        threading.Thread(target=discover, name="provider-models", daemon=True).start()

    def check_provider(self, provider, command=None):
        if not isinstance(provider, str) or provider not in PROVIDER_NAMES:
            raise ValueError("Choose Codex or Claude CLI.")
        with self.lock:
            if self.stopping:
                return
            values = dict(self.config, provider=provider)
            if command is not None:
                values[provider + "_command"] = validate_command(command)
            config = agent_config_for(values)
            requested = list(config.command)
            previous = self.providers.get(provider, {})
            if previous.get("checking") and previous.get("requested_command") == requested:
                return
            entry = {"ready": False, "checking": True, "check_id": secrets.token_hex(12),
                     "requested_command": requested, "message": "Checking installed CLI and sign-in…"}
            self.providers[provider] = entry
        def check():
            from .agent import check_provider
            try:
                result = check_provider(config)
            except Exception:
                result = {"ready": False, "message": "Could not check the CLI. Verify its executable path."}
            with self.lock:
                if self.providers.get(provider) is entry:
                    self.providers[provider] = dict(entry, **result, checking=False)
                    if result.get("ready"):
                        self.refresh_models(provider, result.get("command", requested))
        threading.Thread(target=check, name="provider-status", daemon=True).start()

    @staticmethod
    def prune_config_backups(directory, keep=MAX_CONFIG_BACKUPS):
        """Keep only the newest ``keep`` worldprompt.backup-*.json files; never fails the connect."""
        try:
            backups = sorted(((path.stat().st_mtime_ns, path.name, path)
                              for path in Path(directory).glob("worldprompt.backup-*.json") if path.is_file()))
        except OSError:
            return
        for _, _, path in backups[:max(0, len(backups) - keep)]:
            try:
                path.unlink()
            except OSError:
                pass

    def adapter_token_file(self, directory):
        """The token path as the mod must read it for an instance in ``directory``."""
        token_file = str(self.token_path.resolve())
        # Windows Java needs a Windows-readable path when Core runs in WSL and the
        # instance is on a Windows drive. Elsewhere /mnt/ is an ordinary mount
        # (for example on a VPS) and the path is used as is.
        if wsl.is_wsl() and PurePosixPath(directory).as_posix().startswith("/mnt/"):
            token_file = wsl.wslpath("-w", token_file)
        return token_file

    def connect_minecraft(self, folder):
        if not isinstance(folder, str) or not folder.strip() or "\x00" in folder or len(folder) > 4096:
            raise ValueError("Choose an existing Minecraft instance directory containing a mods folder.")
        directory = Path(normalize_folder(folder))
        if not directory.is_dir() or not (directory / "mods").is_dir():
            raise ValueError("Choose an existing Minecraft instance directory containing a mods folder.")
        destination = directory / "config/worldprompt.json"
        existing = json.loads(destination.read_text(encoding="utf-8-sig")) if destination.exists() else {}
        if not isinstance(existing, dict):
            raise ValueError("Minecraft's WorldPrompt config must be an object.")
        token_file = self.adapter_token_file(directory)
        if destination.exists():
            backup = destination.with_name(f"worldprompt.backup-{time.time_ns()}.json")
            write_private_json(backup, existing)
            self.prune_config_backups(destination.parent)
        existing.update(serviceUrl=self.world_url, tokenFile=token_file)
        existing.pop("token", None)
        write_private_json(destination, existing)
        return {"message": "Minecraft connection saved. Restart Minecraft to load its updated configuration.", "path": str(destination)}


def make_handler(app):
    class Handler(BaseHTTPRequestHandler):
        server_version = "WorldPromptCore/" + __version__

        def setup(self):
            super().setup()
            self.connection.settimeout(15)

        def log_message(self, *args):
            pass

        def send(self, status, data, content_type="application/json; charset=utf-8"):
            if status >= 400:
                discard_rejected_body(self, 64000)
            payload = json.dumps(data).encode("utf-8") if isinstance(data, (dict, list)) else data
            self.send_response(status)
            for key, value in {"Content-Type": content_type, "Content-Length": str(len(payload)),
                               "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff",
                               "Content-Security-Policy": "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"}.items():
                self.send_header(key, value)
            self.end_headers()
            self.wfile.write(payload)

        def valid_host(self):
            try:
                host = urlparse("http://" + self.headers.get("Host", ""))
                return host.hostname in {"127.0.0.1", "localhost", "::1"} and host.port == self.server.server_port
            except ValueError:
                return False

        def do_GET(self):
            if not self.valid_host():
                return self.send(403, {"error": "Local connections only."})
            path = urlparse(self.path).path
            if path == "/api/state":
                session = self.headers.get("X-WorldPrompt-Session", "")
                if not hmac.compare_digest(session.encode(), app.session.encode()):
                    return self.send(403, {"error": "Open the private Core settings URL to access this service."})
                return self.send(200, app.snapshot())
            if path not in STATIC_FILES:
                return self.send(404, {"error": "Not found."})
            name, content_type = STATIC_FILES[path]
            return self.send(200, (STATIC_DIR / name).read_bytes(), content_type)

        def do_POST(self):
            if not self.valid_host():
                return self.send(403, {"error": "Local connections only."})
            origin = self.headers.get("Origin")
            session = self.headers.get("X-WorldPrompt-Session", "")
            if ((origin and origin != "http://" + self.headers.get("Host", "")) or
                    self.headers.get("Sec-Fetch-Site") == "cross-site" or
                    not hmac.compare_digest(session.encode(), app.session.encode())):
                return self.send(403, {"error": "Reload the Core window before continuing."})
            if self.headers.get("Content-Type", "").split(";")[0] != "application/json":
                return self.send(415, {"error": "Use application/json."})
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 1 <= length <= 64000:
                    return self.send(413, {"error": "Invalid request size."})
                self.body_read = True
                body = json.loads(self.rfile.read(length))
                if not isinstance(body, dict):
                    raise ValueError("Expected a JSON object.")
                if self.path == "/api/config":
                    return self.send(200, app.update_config(body))
                if self.path == "/api/providers/check":
                    app.check_provider(body.get("provider"), body.get("command"))
                elif self.path == "/api/providers/models":
                    app.refresh_models(body.get("provider"), body.get("command"))
                elif self.path == "/api/minecraft/connect":
                    return self.send(200, app.connect_minecraft(body.get("folder", "")))
                elif self.path == "/api/jobs/remove":
                    removed = app.supervisor.remove_jobs(body.get("job_ids"))
                    return self.send(200, {"removed": removed, "state": app.snapshot()})
                elif self.path == "/api/shutdown":
                    app.stopping = True
                    self.send(200, {"message": "Core is stopping. Active jobs will be cancelled."})
                    if app.shutdown_callback:
                        threading.Thread(target=app.shutdown_callback, daemon=True).start()
                    return
                else:
                    return self.send(404, {"error": "Not found."})
                return self.send(202, app.snapshot())
            except (ValueError, TypeError, OSError) as exc:
                return self.send(400, {"error": str(exc)})
            except Exception:
                return self.send(500, {"error": "Core could not complete this action."})
    return Handler


def serve(config, port=None, config_path=None, open_browser=True, tray_enabled=True):
    from .world_jobs import JobSupervisor
    from .world_server import adapter_token, make_world_handler
    config = validate_config(config)
    if port is not None and (type(port) is not int or not 0 <= port <= 65535):
        raise ValueError("The GUI port override must be between 0 and 65535.")
    if port == config["world_port"]:
        raise ValueError("The settings and Minecraft ports must be different.")
    supervisor = JobSupervisor(config)
    gui = world = app = None
    world_thread = None
    discovery = None
    gui_access = None
    tray = None
    previous_sigterm = None
    try:
        token, token_path = adapter_token(supervisor.directory, config.get("world_token_file") or None)
        world = ThreadingHTTPServer(("127.0.0.1", config["world_port"]), make_world_handler(supervisor, token))
        world.daemon_threads = True
        world_url = f"http://127.0.0.1:{world.server_port}"
        app = CoreApp(config, supervisor, token_path, world_url, config_path)
        gui = ThreadingHTTPServer(("127.0.0.1", config["port"] if port is None else port), make_handler(app))
        gui.daemon_threads = True
        gui_access = gui_access_path(config_path)
        write_private_json(gui_access, {"session": app.session, "port": gui.server_port})
        def shutdown():
            app.stopping = True
            gui.shutdown()
        app.shutdown_callback = shutdown
        discovery = {"serviceUrl": world_url, "tokenFile": str(token_path.resolve())}
        write_private_json(connection_path(), discovery)
        world_thread = threading.Thread(target=world.serve_forever, name="minecraft-api", daemon=True)
        world_thread.start()
        url = f"http://127.0.0.1:{gui.server_port}/#session={app.session}"
        print(f"WorldPrompt Core {__version__}: {url}", flush=True)
        print(f"Minecraft service: {world_url}", flush=True)
        app.check_provider(config["provider"])
        if os.name == "nt" and tray_enabled:
            from .tray import SystemTray
            tray = SystemTray(url, shutdown)
            tray.start()
        if threading.current_thread() is threading.main_thread():
            previous_sigterm = signal.getsignal(signal.SIGTERM)
            signal.signal(signal.SIGTERM, lambda *_: threading.Thread(target=shutdown, daemon=True).start())
        if open_browser:
            threading.Thread(target=webbrowser.open, args=(url,), daemon=True).start()
        gui.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        if previous_sigterm is not None:
            signal.signal(signal.SIGTERM, previous_sigterm)
        if tray:
            tray.stop()
        if app:
            app.stopping = True
        if world_thread:
            world.shutdown()
            world_thread.join(timeout=3)
        if world:
            world.server_close()
        if gui:
            gui.server_close()
        supervisor.close()
        if discovery:
            try:
                path = connection_path()
                if json.loads(path.read_text()) == discovery:
                    path.unlink()
            except (OSError, ValueError):
                pass
        if gui_access and app:
            try:
                access = json.loads(gui_access.read_text(encoding="utf-8"))
                if isinstance(access, dict) and access.get("session") == app.session:
                    gui_access.unlink()
            except (OSError, ValueError):
                pass
