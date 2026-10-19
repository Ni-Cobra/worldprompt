"""One immutable world snapshot, one planner lifecycle, one disposable process."""
from __future__ import annotations

import json
from functools import partial
import os
from pathlib import Path
import signal

import numpy as np

from .block_state import parse_state
from .core import BlockVolume
from .limits import MAX_AXIS, MAX_BLOCKS, MAX_CAPTURE_PALETTE, MAX_MESSAGE_CHARS
from .pipeline import agent_config_for
from .agent import AgentConfig, generate_plan
from .block_registry import catalog_from_wire, using_catalog


def snapshot_volume(source: dict) -> BlockVolume:
    """Validate the adapter's x-fastest local volume without accessing a world."""
    if not isinstance(source, dict):
        raise ValueError("Source must be a bounded volume object.")
    dimensions = source.get("dimensions")
    if (not isinstance(dimensions, list) or len(dimensions) != 3 or
            any(type(n) is not int or not 1 <= n <= MAX_AXIS for n in dimensions)):
        raise ValueError(f"Dimensions must be three positive integers, at most {MAX_AXIS} each.")
    w, h, length = dimensions
    if w * h * length > MAX_BLOCKS:
        raise ValueError(f"Selection exceeds {MAX_BLOCKS:,} blocks.")
    palette, blocks = source.get("palette"), source.get("blocks")
    if not isinstance(palette, list) or not 1 <= len(palette) <= min(MAX_BLOCKS, MAX_CAPTURE_PALETTE):
        raise ValueError("Invalid source palette size.")
    for state in palette:
        parse_state(state)
    if len(set(palette)) != len(palette):
        raise ValueError("Source palette contains duplicate states.")
    if not isinstance(blocks, list) or len(blocks) != w * h * length:
        raise ValueError("Source block count does not match dimensions.")
    if any(type(n) is not int or not 0 <= n < len(palette) for n in blocks):
        raise ValueError("Source blocks reference an invalid palette entry.")
    version = source.get("data_version", 0)
    if type(version) is not int or not 0 <= version <= 2**31 - 1:
        raise ValueError("Invalid Minecraft data version.")
    return BlockVolume(np.array(blocks, dtype=np.uint32).reshape(h, length, w), palette, version)


def run_worker(config: dict, request_path, connection):
    # POSIX SIGTERM unwinds the planner; Windows uses a kernel job below.
    def interrupted(signum, frame):
        raise SystemExit("WorldPrompt worker cancelled")

    signal.signal(signal.SIGTERM, interrupted)
    if os.name == "posix":
        # Everything this disposable process creates (previews, replay, logs) stays private;
        # the supervisor no longer changes the umask of the long-lived Core process.
        os.umask(0o077)
        # Linux/WSL: also unwind if the supervisor is abruptly killed.
        import ctypes
        parent = config.get("_supervisor_pid", os.getppid())
        try:
            ctypes.CDLL(None).prctl(1, signal.SIGTERM)
        except AttributeError:
            pass
        if os.getppid() != parent:
            raise SystemExit("Supervisor exited")

    def progress(**values):
        connection.send({"kind": "progress", "values": values})

    try:
        from .process_lifetime import contain_windows_worker
        _windows_job = contain_windows_worker()
        from .pipeline import plan_volume
        request_path = Path(request_path)
        request = json.loads(request_path.read_text(encoding="utf-8"))
        directory = request_path.parent
        catalog = catalog_from_wire(request["block_catalog"]) if "block_catalog" in request else None
        source = snapshot_volume(request["source"])
        extra_context = {"viewpoint_context": request["viewpoint_context"]} if request.get("viewpoint_context") is not None else {}
        if request.get("lecterns"):
            extra_context["lecterns"] = request["lecterns"]
        planner = AgentConfig(**config["_agent_config"]) if "_agent_config" in config else agent_config_for(config, request["effort"])
        def process_started(pid):
            connection.send({"kind": "process", "pid": pid})
        def process_finished(pid):
            connection.send({"kind": "process_finished", "pid": pid})
        with using_catalog(catalog):
            result = plan_volume(source, request["prompt"], directory / "planning",
                                 directory / "previews", planner,
                                 effort=request["effort"], progress=progress, replay_dir=directory / "replay",
                                 planner=partial(generate_plan, source_volume=source,
                                                 process_started=process_started, process_finished=process_finished),
                                 **extra_context)
        (directory / "report.json").write_text(json.dumps(result.report), encoding="utf-8")
        connection.send({"kind": "ready", "manifest": result.manifest})
    except Exception as exc:
        connection.send({"kind": "failed", "message": str(exc)[:MAX_MESSAGE_CHARS]})
    finally:
        connection.close()
