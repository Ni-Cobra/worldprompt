"""Authenticated loopback HTTP adapter for the shared world job supervisor."""
from __future__ import annotations

import hmac
from http.server import BaseHTTPRequestHandler
import json
import os
from pathlib import Path
import re
import secrets
import stat
import sys
from urllib.parse import urlparse

from . import __version__
from .world_jobs import JobNotFound, PlayerJobLimitError
from .http_utils import discard_rejected_body
from .limits import MAX_WORLD_BODY

# Core sends its version on every response; the mod sends its own on every request.
VERSION_HEADER = "X-WorldPrompt-Version"
MOD_VERSION_HEADER = "X-WorldPrompt-Mod-Version"
_VERSION = re.compile(r"(\d{1,9})\.(\d{1,9})\.\d{1,9}(?:[-+][0-9A-Za-z.+-]{1,64})?")


def api_version(version):
    """The MAJOR.MINOR that Core and the mod must share (their API), or None if malformed."""
    match = _VERSION.fullmatch(version) if isinstance(version, str) else None
    return f"{int(match[1])}.{int(match[2])}" if match else None


def version_mismatch(mod_version):
    """None when the mod speaks Core's API version, else the message the player sees."""
    core = api_version(__version__)
    if mod_version is None:
        return (f"This WorldPrompt mod is older than Core {__version__} and does not report its version. "
                f"Install WorldPrompt mod {core}.x to match Core.")
    mod = api_version(mod_version)
    if mod == core:
        return None
    if mod is None:
        return f"This WorldPrompt mod reports an unreadable version. Install WorldPrompt mod {core}.x to match Core {__version__}."
    return (f"WorldPrompt mod {mod_version} does not work with Core {__version__}: the first two version numbers "
            f"must match. Install mod {core}.x, or Core {mod}.x.")


def token_permission_warning(path: Path):
    """Describe how a POSIX token file is exposed beyond its owner/group, or return None.

    Group access is the documented way to share the token with a Minecraft server
    running as another user; access for everyone, or write access for the group,
    is not.
    """
    if os.name != "posix":
        return None
    try:
        mode = stat.S_IMODE(path.stat().st_mode)
    except OSError:
        return None
    if mode & 0o007:
        return (f"Adapter token file {path} is accessible to every user on this machine (mode {mode:04o}). "
                f"Any local user could submit jobs as your Minecraft server; run chmod 600 on it, or 640 "
                f"with a shared group if Minecraft runs as another user.")
    if mode & 0o020:
        return (f"Adapter token file {path} is writable by its group (mode {mode:04o}), so group members could "
                f"replace the token. Use chmod 600, or 640 with a shared group.")
    return None


def adapter_token(directory: Path, token_file=None):
    path = Path(token_file).expanduser() if token_file else directory / "adapter-token.txt"
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        # Created private from the first byte, whatever the caller's umask.
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(secrets.token_urlsafe(32) + "\n")
        os.chmod(path, 0o600)
    except FileExistsError:
        pass
    token = path.read_text(encoding="utf-8").strip()
    if len(token) < 24 or not token.isascii() or any(c.isspace() for c in token):
        raise ValueError("Adapter token must be at least 24 ASCII characters without spaces.")
    warning = token_permission_warning(path)
    if warning:
        print("WorldPrompt Core warning: " + warning, file=sys.stderr, flush=True)
    return token, path


def make_world_handler(supervisor, token):
    class Handler(BaseHTTPRequestHandler):
        server_version = "WorldPrompt/1"

        def setup(self):
            super().setup()
            self.connection.settimeout(30)

        def log_message(self, format, *args):
            # Polling must not create an unbounded access log or expose requests.
            pass

        def send(self, status, value):
            if status >= 400:
                discard_rejected_body(self, MAX_WORLD_BODY)
            data = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header(VERSION_HEADER, __version__)
            self.end_headers()
            self.wfile.write(data)

        def authorized(self):
            try:
                host = urlparse("http://" + self.headers.get("Host", ""))
                if host.hostname not in {"localhost", "127.0.0.1", "::1"} or host.port != self.server.server_port:
                    return False
            except ValueError:
                return False
            authorization = self.headers.get("Authorization", "")
            return hmac.compare_digest(authorization.encode("utf-8"), ("Bearer " + token).encode("utf-8"))

        def dispatch(self, method):
            try:
                if not self.authorized():
                    return self.send(403, {"error": "A local connection and adapter bearer token are required."})
                if self.headers.get("Origin") or self.headers.get("Sec-Fetch-Site") == "cross-site":
                    return self.send(403, {"error": "Browser-origin requests are not supported by the server adapter."})
                request = urlparse(self.path)
                if method == "GET" and request.path == "/api/world/info":
                    # Always answered, so /wp info can show both versions even when they do not match.
                    return self.send(200, supervisor.info())
                mismatch = version_mismatch(self.headers.get(MOD_VERSION_HEADER))
                if mismatch:
                    return self.send(409, {"error": mismatch, "code": "version_mismatch", "core_version": __version__})
                prefix = "/api/world/jobs"
                if request.path != prefix and not request.path.startswith(prefix + "/"):
                    return self.send(404, {"error": "Not found."})
                body = {}
                if method == "POST":
                    if self.headers.get("Content-Type", "").split(";", 1)[0].strip() != "application/json":
                        return self.send(415, {"error": "Use application/json."})
                    if self.headers.get("Transfer-Encoding"):
                        return self.send(400, {"error": "Use a Content-Length request."})
                    length = int(self.headers.get("Content-Length", "0"))
                    if not 0 < length <= MAX_WORLD_BODY:
                        return self.send(413, {"error": "Request body is empty or too large."})
                    self.body_read = True
                    data = self.rfile.read(length)
                    if len(data) != length:
                        raise ValueError("Incomplete request body.")
                    body = json.loads(data)
                    if not isinstance(body, dict):
                        raise ValueError("Expected a JSON object.")
                if request.path == prefix:
                    if method == "GET":
                        return self.send(200, {"jobs": supervisor.list_jobs(brief=True)})
                    return self.send(202, supervisor.submit(body))
                parts = request.path[len(prefix) + 1:].split("/")
                job_id = parts[0]
                if method == "GET":
                    if len(parts) == 1:
                        return self.send(200, supervisor.get_job(job_id))
                    if parts[1:] == ["result"]:
                        return self.send(200, supervisor.result(job_id))
                else:
                    if parts[1:] == ["application"]:
                        return self.send(200, supervisor.application(job_id, body.get("status"), body.get("operation", 0), body.get("message")))
                    if parts[1:] == ["cancel"]:
                        return self.send(200, supervisor.cancel(job_id, body.get("message")))
                self.send(404, {"error": "Not found."})
            except JobNotFound as exc:
                self.send(404, {"error": str(exc)})
            except PlayerJobLimitError as exc:
                self.send(409, {"error": str(exc), "code": exc.code, "limit": exc.limit})
            except (ValueError, TypeError) as exc:
                self.send(400, {"error": str(exc)})
            except OSError:
                self.send(503, {"error": "Job storage is unavailable. The server has not acknowledged this action."})
            except Exception:
                self.send(500, {"error": "The core could not complete this request."})

        def do_GET(self):
            self.dispatch("GET")

        def do_POST(self):
            self.dispatch("POST")
    return Handler
