"""Run WorldPrompt on an isolated Fabric server and real client.

The lane uses a deterministic local Core API fixture. It does not read provider credentials,
use installed Minecraft saves, or stop Gradle daemons. See README.md for the command list.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import shutil
import socket
import struct
import subprocess
import sys
import threading
import time
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

REPO = Path(__file__).resolve().parents[2]
FABRIC = REPO / "fabric"
WORK = Path(os.environ.get("WORLDPROMPT_LANE_DIR", FABRIC / "build" / "lane")).resolve()
TOKEN = "worldprompt-lane-token-26-3-test-only"
PLAYER = os.environ.get("WORLDPROMPT_LANE_PLAYER", "LanePlayer")
CORE_VERSION = "1.1.0"
LOCK = threading.RLock()
SERVER_OWNER = "worldprompt-runtime-lane-server-v1"


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class FakeCore:
    """Minimal protocol-compatible local fixture; plans a single bounded stone fill."""
    def __init__(self, port=0):
        self.jobs: dict[str, dict] = {}
        self.submissions = 0
        self.port_requested = port
        self.http = self._server()
        self.thread = threading.Thread(target=self.http.serve_forever, daemon=True)

    def _server(self):
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, fmt, *args):
                with open(WORK / "core.log", "a", encoding="utf-8") as stream:
                    stream.write((fmt % args) + "\n")

            def send_json(self, status, body):
                raw = json.dumps(body, separators=(",", ":")).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-WorldPrompt-Version", CORE_VERSION)
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def authorized(self):
                return self.headers.get("Authorization") == f"Bearer {TOKEN}"

            def do_GET(self):
                if not self.authorized():
                    self.send_json(403, {"error": "invalid lane token"})
                    return
                if self.path == "/api/world/info":
                    self.send_json(200, {"version": CORE_VERSION, "provider": "lane", "model": "fixture",
                                         "reasoning_effort": "low", "fast_mode": False,
                                         "active_jobs": 0, "queued_jobs": 0})
                    return
                if self.path == "/api/world/jobs":
                    self.send_json(200, {"jobs": [j for j in owner.jobs.values() if j["status"] not in ("failed", "completed")]})
                    return
                parts = self.path.removeprefix("/api/world/jobs/").split("/")
                job = owner.jobs.get(parts[0])
                if not job:
                    self.send_json(404, {"error": "unknown lane job"})
                elif len(parts) == 1:
                    self.send_json(200, job)
                elif parts[1] == "result" and job["status"] in ("ready", "reconstructing", "completed"):
                    self.send_json(200, owner.result(job))
                else:
                    self.send_json(400, {"error": "unexpected lane request"})

            def do_POST(self):
                if not self.authorized():
                    self.send_json(403, {"error": "invalid lane token"})
                    return
                try:
                    body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))))
                except Exception:
                    self.send_json(400, {"error": "invalid JSON"})
                    return
                if self.path == "/api/world/jobs":
                    with LOCK:
                        owner.submissions += 1
                        job_id = body["request_id"]
                        prompt = body.get("prompt", "")
                        state = "failed" if "lane fail" in prompt.lower() else ("planning" if "lane hold" in prompt.lower() else "ready")
                        job = {"id": job_id, "owner_id": body["owner_id"], "owner_name": body["owner_name"],
                               "effort": body.get("effort", "normal"), "status": state,
                               "message": "Lane fixture reports a deliberate planning failure." if state == "failed" else "",
                               "operation": 0, "operation_count": 2 if "lane warning" in prompt.lower() else 1, "model": "fixture",
                               "reasoning_effort": "low", "fast_mode": False}
                        job["request"] = body
                        owner.jobs[job_id] = job
                        owner.save_metrics()
                    self.send_json(202, job)
                    return
                parts = self.path.removeprefix("/api/world/jobs/").split("/")
                job = owner.jobs.get(parts[0])
                if not job:
                    self.send_json(404, {"error": "unknown lane job"})
                    return
                if len(parts) == 2 and parts[1] == "application":
                    job["status"] = body.get("status", job["status"])
                    job["operation"] = body.get("operation", job.get("operation", 0))
                    job["message"] = body.get("message", "")
                    owner.save_metrics()
                    self.send_json(200, job)
                elif len(parts) == 2 and parts[1] == "cancel":
                    job["status"] = "failed"
                    job["message"] = body.get("message", job.get("message", ""))
                    owner.save_metrics()
                    self.send_json(200, job)
                else:
                    self.send_json(404, {"error": "unknown lane route"})
                    return

        return ThreadingHTTPServer(("127.0.0.1", owner.port_requested), Handler)

    @property
    def port(self):
        return self.http.server_address[1]

    def start(self):
        self.thread.start()

    def serve(self):
        self.http.serve_forever()

    def save_metrics(self):
        (WORK / "core-metrics.json").write_text(json.dumps({"submissions": self.submissions,
                "jobs": [{k: v for k, v in job.items() if k != "request"} for job in self.jobs.values()]}, indent=2) + "\n",
                encoding="utf-8")

    def stop(self):
        self.http.shutdown()
        self.http.server_close()

    @staticmethod
    def result(job):
        request = job["request"]
        width, height, length = map(int, request["source"]["dimensions"])
        volume = width * height * length
        palette = list(request["source"]["palette"])
        block = "minecraft:stone"
        if block not in palette:
            palette.append(block)
        palette_id = palette.index(block)
        operation = {"op": "fill", "min": [0, 0, 0], "max": [width - 1, height - 1, length - 1], "block": block}
        definitions = [operation]
        packed = [{"index": 0, "op": "fill", "run_count": 1, "writes": volume,
                   "dynamic_match": False, "runs_base64": base64.b64encode(struct.pack("<III", 0, volume, palette_id)).decode()}]
        if "lane warning" in request.get("prompt", "").lower():
            invalid = "minecraft:worldprompt_lane_invalid"
            palette.append(invalid)
            invalid_id = palette.index(invalid)
            definitions.insert(0, {**operation, "block": invalid})
            definitions[1] = {**operation, "block": block}
            packed = [
                {"index": 0, "op": "fill", "run_count": 1, "writes": volume,
                 "dynamic_match": False, "runs_base64": base64.b64encode(struct.pack("<III", 0, volume, invalid_id)).decode()},
                {"index": 1, "op": "fill", "run_count": 1, "writes": volume,
                 "dynamic_match": False, "runs_base64": base64.b64encode(struct.pack("<III", 0, volume, palette_id)).decode()},
            ]
        return {"result_version": 2, "run_encoding": "base64-u32le", "dimensions": [width, height, length],
                "palette": palette, "operation_count": len(definitions), "plan": {"version": 1, "summary": "Lane stone fill.",
                "operations": definitions}, "operations": packed,
                "provider": "lane", "model": "fixture", "reasoning_effort": "low", "fast_mode": False}


def append_json(path: Path, value: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(value, separators=(",", ":")) + "\n")


def paths():
    return {"server": WORK / "server", "client": WORK / "client", "hook": WORK / "hook",
            "dump": WORK / "dump", "pids": WORK / "pids.json", "events": WORK / "hook" / "events.jsonl"}


def _inside(child: Path, parent: Path) -> bool:
    child = child.resolve()
    parent = parent.resolve()
    return child != parent and parent in child.parents


def prepare_lane_world(server_dir: Path) -> None:
    """Refuse to clear a world unless this isolated lane created and marked it."""
    server_dir = server_dir.resolve()
    if not _inside(server_dir, WORK):
        raise RuntimeError(f"lane server directory escapes its isolated work root: {server_dir}")
    owner_file = server_dir / ".worldprompt-lane-server.json"
    expected = {"owner": SERVER_OWNER, "server_dir": str(server_dir), "work": str(WORK.resolve())}
    if server_dir.exists():
        if owner_file.is_symlink():
            raise RuntimeError(f"refusing to follow a symlink ownership marker: {owner_file}")
        if owner_file.is_file():
            try:
                observed = json.loads(owner_file.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as error:
                raise RuntimeError(f"invalid lane ownership marker at {owner_file}") from error
            if observed != expected:
                raise RuntimeError(f"lane ownership marker does not match this work root: {owner_file}")
        elif any(server_dir.iterdir()):
            raise RuntimeError(f"refusing to reuse unmarked server directory {server_dir}; set a fresh WORLDPROMPT_LANE_DIR")
    server_dir.mkdir(parents=True, exist_ok=True)
    owner_file.write_text(json.dumps(expected, indent=2) + "\n", encoding="utf-8")
    world = server_dir / "world"
    if world.exists():
        if not _inside(world, server_dir):
            raise RuntimeError(f"lane world path escapes its server directory: {world}")
        world_marker = world / ".worldprompt-lane-owned"
        if world_marker.is_symlink() or not world_marker.is_file() or world_marker.read_text(encoding="utf-8") != SERVER_OWNER + "\n":
            raise RuntimeError(f"refusing to delete unmarked world at {world}; set a fresh WORLDPROMPT_LANE_DIR")
        shutil.rmtree(world)


def mark_lane_world(server_dir: Path) -> None:
    world = server_dir.resolve() / "world"
    if world.exists() and _inside(world, server_dir.resolve()):
        (world / ".worldprompt-lane-owned").write_text(SERVER_OWNER + "\n", encoding="utf-8")


def inspect_artifact(value: str | Path | None) -> tuple[Path | None, dict | None]:
    if value is None:
        return None, None
    artifact = Path(value).expanduser().resolve()
    if not artifact.is_file():
        raise RuntimeError(f"Fabric artifact does not exist: {artifact}")
    try:
        with zipfile.ZipFile(artifact) as archive:
            metadata = json.loads(archive.read("fabric.mod.json"))
    except (OSError, zipfile.BadZipFile, KeyError, json.JSONDecodeError) as error:
        raise RuntimeError(f"Fabric artifact is not readable or has no fabric.mod.json: {artifact}") from error
    if metadata.get("id") != "worldprompt":
        raise RuntimeError(f"expected mod id 'worldprompt' in {artifact}, found {metadata.get('id')!r}")
    digest = hashlib.sha256()
    with artifact.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return artifact, {"path": str(artifact), "sha256": digest.hexdigest(),
                      "mod_id": metadata["id"], "version": metadata.get("version", "unknown")}


def gradle(task: str, dump: bool = False) -> int:
    exe = FABRIC / ("gradlew.bat" if os.name == "nt" else "gradlew")
    args = [str(exe), "--no-configuration-cache", "--console=plain", "--max-workers=2"]
    if dump and task == ":runClient":
        args.append(":laneClasses")
    if dump:
        args += [f"-Dworldprompt.lane.dump={paths()['dump']}", "-I", str(REPO / "tools" / "lane" / "dump-run.init.gradle")]
    args += [task]
    return subprocess.run(args, cwd=FABRIC, check=False).returncode


def dump_dir(kind: str) -> Path:
    return paths()["dump"] / f"worldprompt-run{kind.title()}"


def read_dump(kind: str, name: str) -> str:
    path = dump_dir(kind) / name
    if not path.is_file():
        raise RuntimeError(f"missing run config output: {path}")
    return path.read_text(encoding="utf-8")


def prepared_launch(kind: str, artifact: Path | None, load_worldprompt: bool) -> list[str]:
    """Copy Loom's args/config files and select exactly which mod sources this process loads."""
    jvm = [line.strip() for line in read_dump(kind, "jvmargs.txt").splitlines() if line.strip()]
    config_arg = next((arg for arg in jvm if arg.startswith("-Dfabric.dli.config=")), None)
    argfile_arg = next((arg for arg in jvm if arg.startswith("@")), None)
    if config_arg is None or argfile_arg is None:
        raise RuntimeError(f"Loom {kind} dump lacks the Fabric config or Java argument file")
    config_source = Path(config_arg.split("=", 1)[1])
    argfile_source = Path(argfile_arg[1:])
    raw_args = argfile_source.read_text(encoding="utf-8").splitlines()
    try:
        classpath_index = next(i for i, line in enumerate(raw_args) if line.strip() in ("-classpath", "--class-path"))
    except StopIteration as error:
        raise RuntimeError(f"Loom {kind} argument file has no classpath option") from error
    if classpath_index + 1 >= len(raw_args):
        raise RuntimeError(f"Loom {kind} argument file has an incomplete classpath")

    classpath_line = raw_args[classpath_index + 1].strip()
    quoted = len(classpath_line) > 1 and classpath_line[0] == classpath_line[-1] == '"'
    classpath_line = classpath_line[1:-1] if quoted else classpath_line
    source_roots = [
        FABRIC / "build" / "classes" / "java" / name
        for name in ("main", "lane", "gametest")
    ] + [FABRIC / "build" / "resources" / name for name in ("main", "lane", "gametest")]

    def normalized(path: str | Path) -> str:
        return os.path.normcase(os.path.abspath(os.fspath(path)))

    removed = {normalized(path) for path in source_roots}
    entries = []
    for entry in classpath_line.split(os.pathsep):
        entry = entry.strip().strip('"')
        if entry and normalized(entry) not in removed:
            entries.append(entry)
    groups: list[list[str]] = []
    if load_worldprompt:
        if artifact is not None:
            entries.append(str(artifact))
            groups.append([str(artifact)])
        else:
            main = [str(FABRIC / "build" / "classes" / "java" / "main"),
                    str(FABRIC / "build" / "resources" / "main")]
            entries.extend(main)
            groups.append(main)
    if kind == "client":
        lane = [str(FABRIC / "build" / "classes" / "java" / "lane"),
                str(FABRIC / "build" / "resources" / "lane")]
        entries.extend(lane)
        groups.append(lane)
    classpath = os.pathsep.join(entries)
    if quoted or " " in classpath:
        classpath = '"' + classpath + '"'
    raw_args[classpath_index + 1] = classpath

    launch_dir = WORK / "launch" / ("jar-" + artifact.stem if artifact else "dev")
    launch_dir.mkdir(parents=True, exist_ok=True)
    custom_args = launch_dir / f"{kind}.args"
    custom_args.write_text("\n".join(raw_args) + "\n", encoding="utf-8")
    config = config_source.read_text(encoding="utf-8")
    group_line = "\tfabric.classPathGroups=" + ";;".join(";".join(group) for group in groups)
    config_lines = config.splitlines()
    for i, line in enumerate(config_lines):
        if line.lstrip().startswith("fabric.classPathGroups="):
            config_lines[i] = group_line
            break
    else:
        raise RuntimeError(f"Loom {kind} config lacks fabric.classPathGroups")
    custom_config = launch_dir / f"{kind}-launch.cfg"
    custom_config.write_text("\n".join(config_lines) + "\n", encoding="utf-8")

    result = []
    for arg in jvm:
        if arg.startswith("-Dfabric.dli.config="):
            result.append(f"-Dfabric.dli.config={custom_config}")
        elif arg.startswith("@"):
            result.append("@" + str(custom_args))
        else:
            result.append(arg)
    return result


def launch(kind: str, cwd: Path, extras: list[str], args: list[str], log: Path,
           *, artifact: Path | None = None, load_worldprompt: bool = True):
    java = read_dump(kind, "launcher.txt").strip()
    if java == "none" or not Path(java).is_file():
        java = shutil.which("java") or "java"
    jvm = prepared_launch(kind, artifact, load_worldprompt)
    command = [java, *jvm, *extras, read_dump(kind, "main.txt").strip(), *args]
    output = log.open("w", encoding="utf-8")
    kwargs = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt" else {"start_new_session": True}
    return subprocess.Popen(command, cwd=cwd, stdin=subprocess.DEVNULL, stdout=output,
                            stderr=subprocess.STDOUT, **kwargs)


def wait_log(log: Path, needle: str, seconds=240):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if log.exists() and needle in log.read_text(encoding="utf-8", errors="replace"):
            return
        time.sleep(1)
    raise RuntimeError(f"timed out waiting for {needle!r} in {log}")


def load_pids():
    return json.loads(paths()["pids"].read_text(encoding="utf-8"))


def save_pids(data):
    paths()["pids"].write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def up(width: int, height: int, artifact_path: str | None = None, client_mod: bool = False):
    p = paths()
    if p["pids"].exists():
        raise RuntimeError("lane PID records already exist; run `down` and inspect any skipped PID before starting again")
    artifact, artifact_info = inspect_artifact(artifact_path)
    for directory in (p["server"], p["client"], p["hook"]):
        directory.mkdir(parents=True, exist_ok=True)
    if not (dump_dir("client") / "jvmargs.txt").is_file() or not (dump_dir("server") / "jvmargs.txt").is_file():
        raise RuntimeError("run `python tools/lane/lane.py gen` after the Fabric build first")
    core_port = free_port()
    prepare_lane_world(p["server"])
    (p["server"] / "eula.txt").write_text("eula=true\n", encoding="utf-8")
    props = {"online-mode": "false", "enforce-secure-profile": "false", "server-ip": "127.0.0.1",
             "server-port": str(free_port()), "gamemode": "creative", "force-gamemode": "true",
             "difficulty": "peaceful", "level-type": "minecraft:flat", "generate-structures": "false",
             "spawn-protection": "0", "view-distance": "6", "simulation-distance": "4", "max-players": "2",
             "white-list": "false", "enforce-whitelist": "false",
             "enable-rcon": "true", "rcon.port": str(free_port()), "rcon.password": TOKEN,
             "broadcast-rcon-to-ops": "false", "motd": "WorldPrompt isolated lane"}
    (p["server"] / "server.properties").write_text("".join(f"{k}={v}\n" for k, v in props.items()), encoding="utf-8")
    config = {"serviceUrl": f"http://127.0.0.1:{core_port}", "tokenFile": "", "token": TOKEN,
              "maxSelectionBlocks": 2000000, "blocksPerTick": 65536, "pollMilliseconds": 250,
              "maxParticleSegments": 96, "selectionCubeSize": 30, "maxUndoEdits": 16, "summaryBook": False}
    (p["server"] / "config").mkdir(exist_ok=True)
    (p["server"] / "config" / "worldprompt.json").write_text(json.dumps(config, indent=2), encoding="utf-8")
    (p["hook"] / "commands.jsonl").write_text("", encoding="utf-8")
    (p["hook"] / "events.jsonl").write_text("", encoding="utf-8")
    core = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "core", "--port", str(core_port)],
                            cwd=REPO, stdin=subprocess.DEVNULL, stdout=(WORK / "core.log").open("w", encoding="utf-8"),
                            stderr=subprocess.STDOUT,
                            **({"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt" else {"start_new_session": True}))
    save_pids({"core": core.pid, "core_created": None,
               "core_marker": f"lane.py core --port {core_port}", "core_port": core_port,
               "rcon_port": int(props["rcon.port"]), "game_port": int(props["server-port"]),
               "artifact": artifact_info, "client_has_worldprompt": bool(client_mod),
               "server_loader": "fabric"})
    core_created = process_identity(core.pid)
    pids = load_pids()
    pids["core_created"] = core_created
    save_pids(pids)
    try:
        wait_for(lambda: can_connect(core_port), 20, "fake Core did not start")
        server_marker = f"-Dworldprompt.lane.server={p['server']}"
        server = launch("server", p["server"], ["-Xmx768M", "-Xms256M", server_marker], ["nogui"],
                        WORK / "server.log", artifact=artifact, load_worldprompt=True)
        pids = load_pids()
        pids.update({"server": server.pid, "server_created": None, "server_marker": server_marker})
        save_pids(pids)
        pids["server_created"] = process_identity(server.pid)
        save_pids(pids)
        wait_log(WORK / "server.log", "Done (", 300)
        mark_lane_world(p["server"])
        server_address = f"127.0.0.1:{props['server-port']}"
        client_artifact = artifact if client_mod else None
        client = launch("client", p["client"], ["-Xmx1536M", "-Xms512M", f"-Dworldprompt.lane={p['hook']}",
                                                 f"-Dworldprompt.lane.serverAddress={server_address}"],
                        ["--username", PLAYER, "--width", str(width), "--height", str(height),
                         "--quickPlayMultiplayer", server_address], WORK / "client.log",
                        artifact=client_artifact, load_worldprompt=client_mod)
        pids = load_pids()
        pids.update({"client": client.pid, "client_created": None,
                     "client_marker": f"-Dworldprompt.lane={p['hook']}"})
        save_pids(pids)
        pids["client_created"] = process_identity(client.pid)
        save_pids(pids)
        wait_for(lambda: (p["hook"] / "ready.json").is_file(), 120, "client hook did not initialize")
        wait_for(lambda: PLAYER in rcon("list"), 180, "client did not join the dedicated server")
        rcon(f"op {PLAYER}")
    except Exception:
        down()
        raise
    print(f"lane up: server={server.pid} client={client.pid} fake-core=127.0.0.1:{core_port}; "
          f"client WorldPrompt={'installed' if client_mod else 'absent'}" +
          (f"; jar sha256={artifact_info['sha256']}" if artifact_info else "; development classes"))


def can_connect(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.2):
            return True
    except OSError:
        return False


def wait_for(check, seconds: int, error: str):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            if check():
                return
        except OSError:
            pass
        time.sleep(0.5)
    raise RuntimeError(error)


def rcon(command: str) -> str:
    pids = load_pids()
    port = int(pids["rcon_port"])

    def packet(request_id, kind, payload):
        data = struct.pack("<ii", request_id, kind) + payload.encode() + b"\0\0"
        return struct.pack("<i", len(data)) + data

    def receive(sock):
        header = sock.recv(4)
        if len(header) != 4:
            raise OSError("short RCON reply")
        size = struct.unpack("<i", header)[0]
        data = b""
        while len(data) < size:
            chunk = sock.recv(size - len(data))
            if not chunk:
                raise OSError("RCON closed")
            data += chunk
        return struct.unpack("<ii", data[:8])[0], data[8:-2].decode("utf-8", "replace")

    with socket.create_connection(("127.0.0.1", port), timeout=5) as sock:
        sock.settimeout(5)
        sock.sendall(packet(1, 3, TOKEN))
        if receive(sock)[0] == -1:
            raise OSError("RCON authentication failed")
        sock.sendall(packet(2, 2, command))
        return receive(sock)[1]


def events() -> list[dict]:
    path = paths()["events"]
    if not path.exists():
        return []
    result = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            result.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return result


def command(action: str, *, text="", name="", pos=None, timeout=15) -> str:
    p = paths()
    command_id = f"{int(time.time() * 1000)}-{len(events())}"
    item = {"id": command_id, "action": action}
    if text: item["text"] = text
    if name: item["name"] = name
    if pos:
        item.update(dict(zip(("x", "y", "z"), pos)))
    append_json(p["hook"] / "commands.jsonl", item)
    wait_for(lambda: any(e.get("command_id") == command_id for e in events()), timeout,
             f"client hook did not acknowledge {action} {text}")
    return command_id


def wait_message(needle: str, after: int = 0, seconds=35) -> dict:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        found = [e for e in events()[after:] if e.get("type") in ("message", "chat") and needle.lower() in e.get("text", "").lower()]
        if found:
            return found[-1]
        time.sleep(0.25)
    tail = " | ".join(e.get("text", "") for e in events()[-12:] if e.get("type") in ("message", "chat"))
    raise RuntimeError(f"no client feedback containing {needle!r}; recent feedback: {tail}")


def probe_event(name: str, pos: tuple[int, int, int], timeout=10) -> dict:
    command_id = command("probe", name=name, pos=pos, timeout=timeout)
    wait_for(lambda: any(e.get("type") == "probe" and e.get("command_id") == command_id for e in events()),
             timeout, f"probe {name} produced no result")
    return next(e for e in reversed(events()) if e.get("type") == "probe" and e.get("command_id") == command_id)


def probe(name: str, pos: tuple[int, int, int]) -> str:
    return probe_event(name, pos).get("block", "")


def wait_view(name: str, pos: tuple[int, int, int], expected: str,
              camera: tuple[float, float, float], look: tuple[float, float] = (45, 30), seconds=20) -> dict:
    """Wait until the live client reports both the requested camera and a synced target block."""
    deadline = time.monotonic() + seconds
    latest = None
    while time.monotonic() < deadline:
        latest = probe_event(name, pos, timeout=min(5, max(1, int(deadline - time.monotonic()))))
        at_camera = all(abs(float(latest.get(axis, 1e9)) - value) < 0.05
                        for axis, value in zip(("x", "y", "z"), camera))
        at_look = all(abs(float(latest.get(axis, 1e9)) - value) < 0.5
                      for axis, value in zip(("yaw", "pitch"), look))
        sees_block = expected in latest.get("block", "")
        if at_camera and at_look and sees_block:
            return latest
        time.sleep(0.25)
    raise RuntimeError(f"client view did not converge for {name}; last observation: {latest}")


def screenshot(name: str) -> Path:
    command("screenshot", name=name, timeout=30)
    path = paths()["hook"] / "screenshots" / name
    wait_for(path.is_file, 10, f"Minecraft did not save screenshot {name}")
    return path


def run_smoke():
    if not paths()["pids"].exists():
        raise RuntimeError("start the lane with `python tools/lane/lane.py up` first")
    before = len(events())
    # Give the isolated build a visible ground and wall while keeping the 2x2 target selection unobstructed.
    rcon("fill -2 99 -2 3 99 3 minecraft:stone_bricks")
    rcon("fill -1 100 2 2 104 2 minecraft:quartz_block")
    rcon("fill 0 100 0 1 100 1 minecraft:gold_block")
    rcon(f"gamemode spectator {PLAYER}")
    rcon(f"tp {PLAYER} 5.5 104 -5.5 45 30")
    camera = (5.5, 104.0, -5.5)
    for text in ("wp pos1 0 100 0", "wp pos2 1 100 1"):
        command("command", text=text)
    command("command", text="wp prompt lane smoke fill")
    wait_message("Build complete", before)
    built = wait_view("built", (0, 100, 0), "minecraft:stone", camera)
    built_shot = screenshot("worldprompt-built")
    if "minecraft:stone" not in built["block"]:
        raise RuntimeError(f"server-applied block was not visible to the real client: {built['block']}")
    command("command", text="wp undo")
    wait_message("Selection restored", before)
    undone = wait_view("undone", (0, 100, 0), "minecraft:gold_block", camera)
    undone_shot = screenshot("worldprompt-undone")
    if "minecraft:gold_block" not in undone["block"]:
        raise RuntimeError(f"undo did not restore the source block: {undone['block']}")
    command("command", text="wp rebuild")
    wait_message("Rebuild complete", before)
    rebuilt = wait_view("rebuilt", (0, 100, 0), "minecraft:stone", camera)
    rebuilt_shot = screenshot("worldprompt-rebuilt")
    if "minecraft:stone" not in rebuilt["block"]:
        raise RuntimeError(f"rebuild did not reapply the saved plan: {rebuilt['block']}")
    pids = load_pids()
    # The fixture lives inside the runner process; request counts are written by the fake API below.
    metrics = json.loads((WORK / "core-metrics.json").read_text(encoding="utf-8"))
    if metrics["submissions"] != 1:
        raise RuntimeError(f"rebuild should not create a Core job; fixture saw {metrics['submissions']} submissions")
    report = {"scenario": "smoke", "passed": True, "built": built["block"], "undone": undone["block"],
              "rebuilt": rebuilt["block"], "camera": camera,
              "look": {"yaw": built["yaw"], "pitch": built["pitch"]},
              "screenshots": [str(built_shot), str(undone_shot), str(rebuilt_shot)],
              "client_feedback": [e for e in events()[before:] if e.get("type") in ("message", "chat")],
              "core_submissions_including_rebuild": metrics["submissions"], "core_port": pids["core_port"],
              "artifact": pids.get("artifact"),
              "server_loader": pids.get("server_loader", "fabric"),
              "client_has_worldprompt": pids.get("client_has_worldprompt", False)}
    (WORK / "report.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(f"PASS: build={built['block']}; undo={undone['block']}; rebuild={rebuilt['block']}; "
          f"camera={camera}; screenshots={WORK / 'hook' / 'screenshots'}")


def run_edges():
    if not paths()["pids"].exists():
        raise RuntimeError("start the lane with `python tools/lane/lane.py up` first")
    pids = load_pids()
    metrics_path = WORK / "core-metrics.json"
    before = len(events())
    rcon("fill 4 100 0 5 100 1 minecraft:diamond_block")
    for text in ("wp pos1 4 100 0", "wp pos2 5 100 1", "wp prompt lane fail deliberate"):
        command("command", text=text)
    wait_message("deliberate planning failure", before)
    untouched = probe("failed-job-preserved-source", (4, 100, 0))
    if "minecraft:diamond_block" not in untouched:
        raise RuntimeError(f"failed planning changed the world: {untouched}")
    failure_metrics = json.loads(metrics_path.read_text(encoding="utf-8"))
    if not any(job["status"] == "failed" for job in failure_metrics["jobs"]):
        raise RuntimeError("the fixture did not record the failed plan")

    rcon("fill 12 100 0 13 100 1 minecraft:emerald_block")
    warning_start = len(events())
    for text in ("wp pos1 12 100 0", "wp pos2 13 100 1", "wp prompt lane warning skip unsupported block"):
        command("command", text=text)
    wait_message("Warning: Unsupported block state", warning_start)
    wait_message("Build complete", warning_start)
    warned_result = events()[warning_start:]
    warning_completion = next((e.get("text", "") for e in warned_result
                               if e.get("type") in ("message", "chat") and "Build complete" in e.get("text", "")), "")
    if "block warning" not in warning_completion or "placement(s) skipped" not in warning_completion:
        raise RuntimeError(f"completion did not summarize the invalid palette write: {warning_completion}")
    warning_block = probe("valid-operation-after-warning", (12, 100, 0))
    if "minecraft:stone" not in warning_block:
        raise RuntimeError(f"valid operation did not continue after the unsupported block warning: {warning_block}")

    rcon(f"deop {PLAYER}")
    submissions = json.loads(metrics_path.read_text(encoding="utf-8"))["submissions"]
    denied_start = len(events())
    command("command", text="wp prompt lane permission check")
    time.sleep(2)
    permission_metrics = json.loads(metrics_path.read_text(encoding="utf-8"))
    if permission_metrics["submissions"] != submissions:
        raise RuntimeError("a non-operator prompt reached Core despite the default permission gate")
    permission_feedback = events()[denied_start:]
    rcon(f"op {PLAYER}")

    for text in ("wp pos1 8 100 0", "wp pos2 9 100 1", "wp prompt lane hold disconnect"):
        command("command", text=text)
    wait_for(lambda: any(job.get("status") == "planning" for job in json.loads(metrics_path.read_text(encoding="utf-8")).get("jobs", [])),
             15, "held lane job was not submitted")
    command("disconnect")
    wait_for(lambda: any(job.get("status") == "failed" and "Player disconnected" in job.get("message", "")
                         for job in json.loads(metrics_path.read_text(encoding="utf-8")).get("jobs", [])),
             45, "disconnect did not cancel the real player's held WorldPrompt job")
    report = {"scenario": "runtime edges", "passed": True,
              "failed_plan_left_world_unchanged": untouched,
              "unsupported_palette_warning_then_valid_operation": warning_block,
              "warning_completion": warning_completion,
              "permission_gate_prevented_submission": True,
              "permission_feedback": [e for e in permission_feedback if e.get("type") in ("message", "chat")],
              "disconnect_cancelled_held_job": True,
              "core_metrics": json.loads(metrics_path.read_text(encoding="utf-8")),
              "client_feedback": [e for e in events()[before:] if e.get("type") in ("message", "chat")],
              "core_port": pids["core_port"], "artifact": pids.get("artifact"),
              "server_loader": pids.get("server_loader", "fabric"),
              "client_has_worldprompt": pids.get("client_has_worldprompt", False)}
    (WORK / "edges-report.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(f"PASS: failed plan preserved {untouched}; permission was enforced; disconnect cancelled the held job")


def process_identity(pid: int):
    if os.name == "nt":
        command = f"$p=Get-CimInstance Win32_Process -Filter 'ProcessId = {int(pid)}'; if ($p) {{ $p | Select-Object ProcessId,CreationDate,ExecutablePath | ConvertTo-Json -Compress }}"
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            result = subprocess.run(["powershell", "-NoProfile", "-Command", command], capture_output=True, text=True)
            try:
                data = json.loads(result.stdout)
                if data:
                    return {"creation": str(data["CreationDate"]), "executable": data.get("ExecutablePath", "")}
                if data is None:
                    return None
            except (json.JSONDecodeError, KeyError, TypeError):
                pass
            time.sleep(0.1)
        return None
    try:
        fields = Path(f"/proc/{int(pid)}/stat").read_text().split(")", 1)[1].split()
        return {"creation": fields[19]}
    except (OSError, IndexError):
        return None


def stop_pid(pid: int, identity: dict | None):
    observed = process_identity(pid)
    if not observed or not identity or observed.get("creation") != identity.get("creation"):
        print(f"skipped PID {pid}: it is gone or its recorded process identity changed")
        return observed is None
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True, check=False)
    else:
        try:
            os.killpg(pid, 15)
        except ProcessLookupError:
            return True
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        observed = process_identity(pid)
        if not observed or observed.get("creation") != identity.get("creation"):
            return True
        time.sleep(0.2)
    if os.name != "nt":
        try:
            os.killpg(pid, 9)
        except ProcessLookupError:
            return True
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            observed = process_identity(pid)
            if not observed or observed.get("creation") != identity.get("creation"):
                return True
            time.sleep(0.2)
    print(f"could not confirm lane PID {pid} exited")
    return False


def down():
    p = paths()
    if not p["pids"].exists():
        print("lane is not running")
        return
    data = load_pids()
    try:
        rcon("stop")
    except OSError:
        pass
    remaining = False
    for name in ("client", "server", "core"):
        pid = data.get(name)
        if pid:
            if stop_pid(int(pid), data.get(name + "_created")):
                print(f"stopped or confirmed gone lane {name} PID {pid}")
            else:
                remaining = True
        else:
            continue
    if remaining:
        print("lane PID file retained for any process whose shutdown could not be confirmed")
    else:
        p["pids"].unlink(missing_ok=True)


def status():
    p = paths()
    print(json.dumps({"work": str(WORK), "up": p["pids"].exists(), "events": len(events()),
                      "recent": events()[-8:]}, indent=2))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="verb", required=True)
    sub.add_parser("gen", help="resolve the Loom client and server run configs")
    up_parser = sub.add_parser("up", help="start the isolated server, fake Core, and real client")
    up_parser.add_argument("--width", type=int, default=1280)
    up_parser.add_argument("--height", type=int, default=800)
    up_parser.add_argument("--jar", help="run an already-built WorldPrompt Fabric jar on the server, pinned by SHA-256")
    up_parser.add_argument("--client-mod", action="store_true",
                           help="also load WorldPrompt on the client; default keeps the client mod absent")
    sub.add_parser("smoke", help="assert a real prompt, block placement, undo, and rebuild")
    sub.add_parser("edges", help="assert failed-plan safety, default permissions, and disconnect cleanup")
    sub.add_parser("status", help="show lane telemetry and the recent client events")
    sub.add_parser("down", help="stop only the processes started by this lane")
    core_parser = sub.add_parser("core", help=argparse.SUPPRESS)
    core_parser.add_argument("--port", type=int, required=True)
    args = parser.parse_args()
    WORK.mkdir(parents=True, exist_ok=True)
    try:
        if args.verb == "gen":
            for kind in ("client", "server"):
                if gradle(f":{kind == 'client' and 'runClient' or 'runServer'}", dump=True):
                    return 1
            return 0
        if args.verb == "up": up(args.width, args.height, args.jar, args.client_mod)
        elif args.verb == "smoke": run_smoke()
        elif args.verb == "edges": run_edges()
        elif args.verb == "status": status()
        elif args.verb == "down": down()
        elif args.verb == "core": FakeCore(args.port).serve()
        return 0
    except Exception as error:
        print(f"lane failure: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
