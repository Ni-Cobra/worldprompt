"""Compile authentic per-operation assignments using the one geometry engine.

The Fabric adapter retains these assignments locally instead of independently
implementing JSON geometry, replacement matching, or directional properties.
Operations retain their original order and all writes, including overwrites
and assignments that happen to equal the captured source state.
"""
from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import NamedTuple

import numpy as np

from .core import BlockVolume, apply_plan
from .limits import MAX_OPERATIONS, MAX_REPLAY_BYTES

RUN_BYTES = 12  # little-endian unsigned 32-bit start, length, palette ID


def _runs(positions, palette_ids):
    if not len(positions):
        return np.empty((0, 3), dtype="<u4")
    if np.any(positions[1:] < positions[:-1]):
        order = np.argsort(positions, kind="stable")
        positions, palette_ids = positions[order], palette_ids[order]
    starts = np.r_[0, np.flatnonzero((positions[1:] != positions[:-1] + 1) |
                                   (palette_ids[1:] != palette_ids[:-1])) + 1]
    lengths = np.diff(np.r_[starts, len(positions)])
    return np.column_stack((positions[starts], lengths, palette_ids[starts])).astype("<u4")


class ReplayResult(NamedTuple):
    """A compiled replay plus the outcome of the one application that produced it."""
    manifest: dict
    edited: BlockVolume
    report: dict


def compile_replay(source, plan, destination, *, max_operations=MAX_OPERATIONS) -> ReplayResult:
    """Validate and record each original JSON operation, publishing a manifest last.

    The plan is applied exactly once, and that application is the replay's source:
    ``edited`` and ``report`` are what ``core.apply_plan`` returns for it.

    ``destination`` must be new. Its manifest is the completion marker; failures
    may leave private intermediate files, but no manifest can be served. Fixed
    sized run records keep the complete placement data compact.
    """
    destination = Path(destination)
    destination.mkdir(parents=True)
    operation_dir = destination / "operations"
    operation_dir.mkdir()
    operations = []
    randomized_before = False
    replay_bytes = 0

    def observe(index, operation, positions, palette_ids):
        nonlocal randomized_before, replay_bytes
        dynamic_match = operation["op"] == "replace" and randomized_before
        if dynamic_match:
            # Earlier Fabric choices are unknown here. Visit the entire bounded
            # region and let Fabric match against the live pre-operation state.
            x0, y0, z0 = operation["min"]
            x1, y1, z1 = operation["max"]
            box_width, box_length = x1 - x0 + 1, z1 - z0 + 1
            width, _, length = source.dimensions
            local = np.arange(box_width * box_length * (y1 - y0 + 1), dtype=np.int64)
            positions = ((local % box_width + x0) +
                         (local // box_width % box_length + z0) * width +
                         (local // (box_width * box_length) + y0) * width * length)
            palette_ids = np.zeros(len(positions), dtype=np.uint32)
        runs = _runs(positions, palette_ids)
        replay_bytes += runs.nbytes
        if replay_bytes > MAX_REPLAY_BYTES:
            raise ValueError(f"Complete replay exceeds the {MAX_REPLAY_BYTES >> 20} MiB placement-data limit. Use a smaller selection or simpler plan.")
        details = {"index": index, "op": operation["op"], "run_count": len(runs),
                   "writes": len(positions), "dynamic_match": dynamic_match}
        runs.tofile(operation_dir / f"{index:06d}.runs")
        (operation_dir / f"{index:06d}.json").write_text(json.dumps(details), encoding="utf-8")
        operations.append(details)
        randomized_before |= isinstance(operation["block"], list)

    edited, report = apply_plan(source, plan, max_operations=max_operations, operation_observer=observe)
    manifest = {"version": 1, "dimensions": list(source.dimensions),
                "palette": edited.palette, "operation_count": len(operations),
                "operations": operations,
                "coordinate_order": "x + z * width + y * width * length"}
    (destination / "plan.json").write_text(json.dumps(plan, indent=2), encoding="utf-8")
    (destination / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return ReplayResult(manifest, edited, report)


def read_complete_result(destination):
    """Return one self-contained payload, with compact packed runs for every step."""
    destination = Path(destination)
    result = json.loads((destination / "manifest.json").read_text(encoding="utf-8"))
    result["plan"] = json.loads((destination / "plan.json").read_text(encoding="utf-8"))
    if len(result["operations"]) != result["operation_count"] or len(result["plan"]["operations"]) != result["operation_count"]:
        raise ValueError("Retained replay has inconsistent operation counts.")
    total = 0
    for index, operation in enumerate(result["operations"]):
        path = destination / "operations" / f"{index:06d}.runs"
        expected = operation["run_count"] * RUN_BYTES
        total += expected
        if expected < 0 or total > MAX_REPLAY_BYTES:
            raise ValueError(f"Complete replay exceeds the {MAX_REPLAY_BYTES >> 20} MiB placement-data limit.")
        if path.stat().st_size != expected:
            raise ValueError("Replay operation is truncated or has trailing data.")
        operation["runs_base64"] = base64.b64encode(path.read_bytes()).decode("ascii")
    result.update(result_version=2, run_encoding="base64-u32le")
    return result
