"""Exercise a packaged NeoForge server with the existing vanilla-compatible Fabric lane client."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time
import zipfile

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
os.environ.setdefault("WORLDPROMPT_LANE_DIR", str(REPO / "fabric" / "build" / "lane-neoforge"))
sys.path.insert(0, str(HERE))
import lane as fabric_lane  # noqa: E402

WORK = fabric_lane.WORK


def artifact_info(path: str | Path) -> tuple[Path, dict]:
    artifact = Path(path).expanduser().resolve()
    if not artifact.is_file():
        raise RuntimeError(f"NeoForge artifact does not exist: {artifact}")
    try:
        with zipfile.ZipFile(artifact) as archive:
            text = archive.read("META-INF/neoforge.mods.toml").decode("utf-8")
    except (OSError, zipfile.BadZipFile, KeyError, UnicodeDecodeError) as error:
        raise RuntimeError(f"NeoForge artifact has no readable native metadata: {artifact}") from error
    mods = re.findall(r'(?ms)^\[\[mods\]\]\s*.*?^modId\s*=\s*["\']([^"\']+)', text)
    versions = re.findall(r'(?m)^version\s*=\s*["\']([^"\']+)', text)
    if mods.count("worldprompt") != 1:
        raise RuntimeError(f"expected exactly one worldprompt mod in {artifact}; found {mods}")
    if "${version}" in versions or not versions:
        raise RuntimeError(f"NeoForge artifact metadata has an unresolved version: {versions}")
    digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
    return artifact, {"path": str(artifact), "sha256": digest, "mod_id": "worldprompt",
                      "version": versions[0], "loader": "neoforge"}


def gradle_neoforge_dump(task: str, server_dir: Path) -> int:
    wrapper = REPO / ("gradlew.bat" if os.name == "nt" else "gradlew")
    command = [str(wrapper), "--no-configuration-cache", "--console=plain", "--max-workers=2",
               f"-Dworldprompt.lane.dump={WORK / 'dump'}",
               f"-PworldpromptLaneServerDir={server_dir}", "-I", str(REPO / "tools" / "lane" / "dump-run.init.gradle"),
               task]
    return subprocess.run(command, cwd=REPO, check=False).returncode


def dump_dir() -> Path:
    return WORK / "dump" / "neoforge-runPackagedServer"


def stage_artifact(artifact: Path, info: dict, mods: Path) -> None:
    mods.mkdir(parents=True, exist_ok=True)
    marker = mods / ".worldprompt-lane-jar.json"
    if marker.is_symlink():
        raise RuntimeError(f"refusing to follow a symlink jar marker: {marker}")
    jars = list(mods.glob("*.jar"))
    if marker.exists():
        try:
            previous = json.loads(marker.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise RuntimeError(f"invalid lane jar marker at {marker}") from error
        if previous.get("owner") != fabric_lane.SERVER_OWNER:
            raise RuntimeError(f"jar staging marker is not owned by this lane: {marker}")
        previous_path = (mods / previous.get("name", "")).resolve()
        if not fabric_lane._inside(previous_path, mods):
            raise RuntimeError(f"lane jar marker points outside the isolated mods folder: {previous_path}")
        if (mods / previous.get("name", "")).is_symlink():
            raise RuntimeError(f"refusing to replace a symlink staged jar: {mods / previous.get('name', '')}")
        if previous_path.is_file():
            digest = hashlib.sha256(previous_path.read_bytes()).hexdigest()
            if digest != previous.get("sha256"):
                raise RuntimeError(f"previous lane jar changed after staging; refusing to replace {previous_path}")
            previous_path.unlink()
        jars = list(mods.glob("*.jar"))
    if jars:
        raise RuntimeError(f"unexpected jar(s) in isolated NeoForge mods directory {mods}: {jars}")
    staged = mods / artifact.name
    shutil.copy2(artifact, staged)
    marker.write_text(json.dumps({"owner": fabric_lane.SERVER_OWNER, "name": staged.name,
                                  "sha256": info["sha256"]}, indent=2) + "\n", encoding="utf-8")


def gen() -> None:
    WORK.mkdir(parents=True, exist_ok=True)
    # The client is a real Fabric client carrying only Fabric API and the test hook.
    if fabric_lane.gradle(":runClient", dump=True) != 0 or fabric_lane.gradle(":runServer", dump=True) != 0:
        raise RuntimeError("Fabric lane launch configuration dump failed")
    server_dir = WORK / "server"
    if gradle_neoforge_dump(":neoforge:runServer", server_dir) != 0:
        raise RuntimeError("NeoForge ordinary-server launch configuration dump failed")
    if gradle_neoforge_dump(":neoforge:runPackagedServer", server_dir) != 0:
        raise RuntimeError("NeoForge packaged-server launch configuration dump failed")
    needed = ("jvmargs.txt", "args.txt", "main.txt", "launcher.txt", "classpath.txt", "environment.json")
    missing = [name for name in needed if not (dump_dir() / name).is_file()]
    if missing:
        raise RuntimeError(f"NeoForge run dump is missing {missing}")
    normal = WORK / "dump" / "neoforge-runServer" / "environment.json"
    if not normal.is_file():
        raise RuntimeError("NeoForge ordinary-server environment dump is missing")
    normal_environment = json.loads(normal.read_text(encoding="utf-8"))
    mod_classes = normal_environment.get("MOD_CLASSES")
    if mod_classes is None:
        raise RuntimeError("NeoForge ordinary server did not expose its resolved MOD_CLASSES")
    if "gametest" in mod_classes.lower():
        raise RuntimeError(f"NeoForge ordinary server mod coordinates include GameTest output: {mod_classes}")
    print(f"NeoForge and vanilla-compatible client launch configurations saved under {WORK / 'dump'}")


def launch_server(server_dir: Path, log: Path) -> subprocess.Popen:
    root = dump_dir()
    jvm_args = [line.strip() for line in (root / "jvmargs.txt").read_text(encoding="utf-8").splitlines()
                if line.strip()]
    app_args = [line.strip() for line in (root / "args.txt").read_text(encoding="utf-8").splitlines()
                if line.strip()]
    classpath = (root / "classpath.txt").read_text(encoding="utf-8").strip()
    main = (root / "main.txt").read_text(encoding="utf-8").strip()
    java = (root / "launcher.txt").read_text(encoding="utf-8").strip()
    if not classpath or not main:
        raise RuntimeError("NeoForge launch dump has an empty classpath or main class")
    if java == "none" or not Path(java).is_file():
        java = shutil.which("java") or "java"
    process_env = os.environ.copy()
    process_env.update(json.loads((root / "environment.json").read_text(encoding="utf-8")))
    process_env["MOD_CLASSES"] = ""
    command = [java, "-Xmx1024M", "-Xms256M", *jvm_args, "-classpath", classpath, main, *app_args]
    output = log.open("w", encoding="utf-8")
    kwargs = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt" else {"start_new_session": True}
    return subprocess.Popen(command, cwd=server_dir, env=process_env, stdin=subprocess.DEVNULL,
                            stdout=output, stderr=subprocess.STDOUT, **kwargs)


def up(artifact_path: str, width: int, height: int) -> None:
    p = fabric_lane.paths()
    if p["pids"].exists():
        raise RuntimeError("lane PID records already exist; run `down` and inspect any skipped PID first")
    artifact, info = artifact_info(artifact_path)
    if not (dump_dir() / "jvmargs.txt").is_file():
        raise RuntimeError("run `python tools/lane/neoforge_lane.py gen` after the loader builds first")
    for directory in (p["server"], p["client"], p["hook"]):
        directory.mkdir(parents=True, exist_ok=True)
    core_port = fabric_lane.free_port()
    server_port, rcon_port = fabric_lane.free_port(), fabric_lane.free_port()
    server_dir = p["server"]
    fabric_lane.prepare_lane_world(server_dir)
    (server_dir / "eula.txt").write_text("eula=true\n", encoding="utf-8")
    props = {"online-mode": "false", "enforce-secure-profile": "false", "server-ip": "127.0.0.1",
             "server-port": str(server_port), "gamemode": "creative", "force-gamemode": "true",
             "difficulty": "peaceful", "level-type": "minecraft:flat", "generate-structures": "false",
             "spawn-protection": "0", "view-distance": "6", "simulation-distance": "4", "max-players": "2",
             "white-list": "false", "enforce-whitelist": "false", "enable-rcon": "true",
             "rcon.port": str(rcon_port), "rcon.password": fabric_lane.TOKEN,
             "broadcast-rcon-to-ops": "false", "motd": "WorldPrompt isolated NeoForge lane"}
    (server_dir / "server.properties").write_text("".join(f"{k}={v}\n" for k, v in props.items()), encoding="utf-8")
    config = {"serviceUrl": f"http://127.0.0.1:{core_port}", "tokenFile": "", "token": fabric_lane.TOKEN,
              "maxSelectionBlocks": 2000000, "blocksPerTick": 65536, "pollMilliseconds": 250,
              "maxParticleSegments": 96, "selectionCubeSize": 30, "maxUndoEdits": 16, "summaryBook": False}
    (server_dir / "config").mkdir(exist_ok=True)
    (server_dir / "config" / "worldprompt.json").write_text(json.dumps(config, indent=2), encoding="utf-8")
    stage_artifact(artifact, info, server_dir / "mods")
    (p["hook"] / "commands.jsonl").write_text("", encoding="utf-8")
    (p["hook"] / "events.jsonl").write_text("", encoding="utf-8")

    core = subprocess.Popen([sys.executable, str(HERE / "lane.py"), "core", "--port", str(core_port)],
                            cwd=REPO, stdin=subprocess.DEVNULL,
                            stdout=(WORK / "core.log").open("w", encoding="utf-8"), stderr=subprocess.STDOUT,
                            **({"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt" else {"start_new_session": True}))
    fabric_lane.save_pids({"core": core.pid, "core_created": None,
                           "core_marker": f"lane.py core --port {core_port}", "core_port": core_port,
                           "rcon_port": rcon_port, "game_port": server_port, "artifact": info,
                           "server_loader": "neoforge", "client_has_worldprompt": False})
    core_created = fabric_lane.process_identity(core.pid)
    pids = fabric_lane.load_pids()
    pids["core_created"] = core_created
    fabric_lane.save_pids(pids)
    try:
        fabric_lane.wait_for(lambda: fabric_lane.can_connect(core_port), 20, "fake Core did not start")
        server = launch_server(server_dir, WORK / "server.log")
        pids = fabric_lane.load_pids()
        pids.update({"server": server.pid, "server_created": None,
                     "server_marker": "NeoForge packaged run with staged mods jar"})
        fabric_lane.save_pids(pids)
        pids["server_created"] = fabric_lane.process_identity(server.pid)
        fabric_lane.save_pids(pids)
        fabric_lane.wait_log(WORK / "server.log", "Done (", 360)
        fabric_lane.mark_lane_world(server_dir)
        address = f"127.0.0.1:{server_port}"
        client = fabric_lane.launch("client", p["client"], ["-Xmx1536M", "-Xms512M",
                                      f"-Dworldprompt.lane={p['hook']}", f"-Dworldprompt.lane.serverAddress={address}"],
                                    ["--username", fabric_lane.PLAYER, "--width", str(width), "--height", str(height),
                                     "--quickPlayMultiplayer", address], WORK / "client.log",
                                    artifact=None, load_worldprompt=False)
        pids = fabric_lane.load_pids()
        pids.update({"client": client.pid, "client_created": None,
                     "client_marker": f"-Dworldprompt.lane={p['hook']}"})
        fabric_lane.save_pids(pids)
        pids["client_created"] = fabric_lane.process_identity(client.pid)
        fabric_lane.save_pids(pids)
        fabric_lane.wait_for(lambda: (p["hook"] / "ready.json").is_file(), 120, "client hook did not initialize")
        fabric_lane.wait_for(lambda: fabric_lane.PLAYER in fabric_lane.rcon("list"), 180,
                             "minimal Fabric client did not join the NeoForge packaged server")
        fabric_lane.rcon(f"op {fabric_lane.PLAYER}")
    except Exception:
        fabric_lane.down()
        raise
    print(f"NeoForge lane up: server={server.pid} Fabric client={client.pid} Core=127.0.0.1:{core_port}; "
          f"client WorldPrompt=absent; jar sha256={info['sha256']}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="verb", required=True)
    sub.add_parser("gen", help="dump the NeoForge packagedServer and vanilla-compatible Fabric client launch configs")
    up_parser = sub.add_parser("up", help="launch the packaged NeoForge jar, fixture Core, and minimal client")
    up_parser.add_argument("--jar", required=True, help="built NeoForge mod jar to stage in the isolated server mods folder")
    up_parser.add_argument("--width", type=int, default=1280)
    up_parser.add_argument("--height", type=int, default=800)
    sub.add_parser("smoke", help="assert prompt, placement, undo, and rebuild against the NeoForge jar")
    sub.add_parser("edges", help="assert failure, permission, and disconnect behavior against the NeoForge jar")
    sub.add_parser("status", help="show lane telemetry")
    sub.add_parser("down", help="stop only processes owned by this lane")
    args = parser.parse_args()
    WORK.mkdir(parents=True, exist_ok=True)
    try:
        if args.verb == "gen": gen()
        elif args.verb == "up": up(args.jar, args.width, args.height)
        elif args.verb == "smoke": fabric_lane.run_smoke()
        elif args.verb == "edges": fabric_lane.run_edges()
        elif args.verb == "status": fabric_lane.status()
        elif args.verb == "down": fabric_lane.down()
        return 0
    except Exception as error:
        print(f"NeoForge lane failure: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
