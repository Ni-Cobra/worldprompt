"""Bounded Minecraft block volumes and transactional geometric editing.

Coordinates are public xyz, arrays are yzx, boxes have inclusive endpoints.
"""
from __future__ import annotations

import json
from functools import lru_cache

import numpy as np

from . import block_registry
from .block_registry import validate_state
from .block_state import VolumeError, parse_state, state_string  # noqa: F401 (VolumeError, state_string re-exported)
from .limits import MAX_AXIS, MAX_BLOCK_CHOICES, MAX_BLOCKS, MAX_OPERATIONS, MAX_PLAN_BYTES, MAX_PLAN_ERRORS, MAX_WORK
from .operations import OPERATIONS, PLAN_VERSION, plan_shape_error, prepare_problem

AIR = {"minecraft:air", "minecraft:cave_air", "minecraft:void_air"}


def bounds_of(mask):
    """Inclusive public ``[[x0, y0, z0], [x1, y1, z1]]`` of a ``(y, z, x)`` boolean mask, or None."""
    if not mask.any():
        return None
    # Per-axis reductions avoid a potentially 96 MiB argwhere array.
    ys = np.flatnonzero(mask.any(axis=(1, 2)))
    zs = np.flatnonzero(mask.any(axis=(0, 2)))
    xs = np.flatnonzero(mask.any(axis=(0, 1)))
    return [[int(xs[0]), int(ys[0]), int(zs[0])], [int(xs[-1]), int(ys[-1]), int(zs[-1])]]


class BlockVolume:
    """A local block array; Minecraft retains world and entity persistence."""

    def __init__(self, blocks, palette, data_version=0):
        self.blocks = np.asarray(blocks, dtype=np.uint32)
        self.palette = list(palette)
        self.data_version = data_version

    @property
    def dimensions(self):
        height, length, width = self.blocks.shape
        return width, height, length

    @classmethod
    def empty(cls, dimensions, data_version=0):
        if (not isinstance(dimensions, (tuple, list)) or len(dimensions) != 3 or
                any(type(v) is not int or not 1 <= v <= MAX_AXIS for v in dimensions)):
            raise VolumeError(f"Dimensions must be three positive integers, at most {MAX_AXIS} each.")
        width, height, length = dimensions
        if width * height * length > MAX_BLOCKS:
            raise VolumeError(f"Selection exceeds {MAX_BLOCKS:,} blocks.")
        if type(data_version) is not int or not 0 <= data_version <= 2**31 - 1:
            raise VolumeError("Invalid Minecraft data version.")
        return cls(np.zeros((height, length, width), dtype=np.uint32), ["minecraft:air"], data_version)

    def clone(self):
        return BlockVolume(self.blocks.copy(), self.palette, self.data_version)

    def palette_id(self, state):
        parsed = resolved_state(state)
        try:
            return self.palette.index(state)
        except ValueError:
            # Property order and omitted Minecraft defaults do not change the
            # actual state. Reuse captured states to avoid false block changes.
            for i, candidate in enumerate(self.palette):
                if resolved_state(candidate) == parsed:
                    return i
            self.palette.append(state)
            return len(self.palette) - 1

    def non_air_mask(self):
        solid = np.array([parse_state(state)[0] not in AIR for state in self.palette])
        return solid[self.blocks]

    def summary(self):
        counts = np.bincount(self.blocks.ravel(), minlength=len(self.palette))
        ordered = sorted(zip(self.palette, counts), key=lambda entry: -int(entry[1]))
        mask = self.non_air_mask()
        return {"dimensions": list(self.dimensions),
                "data_version": self.data_version, "non_air_blocks": int(mask.sum()),
                "non_air_bounds": bounds_of(mask),
                "palette": [{"block": block, "count": int(count)} for block, count in ordered if count][:128],
                "palette_total": len(self.palette), "palette_truncated": len(ordered) > 128}


def coord(value, dimensions, name):
    """Check an integer local xyz coordinate against the volume dimensions."""
    if not isinstance(value, list) or len(value) != 3 or any(type(v) is not int for v in value):
        raise VolumeError(f"{name} must be three integer xyz coordinates")
    if any(v < 0 or v >= dimensions[i] for i, v in enumerate(value)):
        raise VolumeError(f"{name} {value} is outside dimensions {list(dimensions)}")
    return value


@lru_cache(maxsize=block_registry.RESOLVED_STATE_CACHE)
def _resolved_state(state):
    base, properties = parse_state(state)
    return base, block_registry.block_defaults().get(base, {}) | properties


def resolved_state(state):
    """Resolve omitted properties as Minecraft does during placement.

    Both paths are cached, because a large plan resolves the same states over and
    over. The bundled fallback is cached globally per state string; a live catalog
    caches on the catalog itself, so two servers on the same Minecraft version
    never share an entry and a job's cache is released with its catalog.
    """
    if not isinstance(state, str):
        parse_state(state)  # Raises VolumeError; unhashable values must not reach the cache.
    catalog = block_registry.current_catalog()
    if catalog is None:
        base, properties = _resolved_state(state)
    else:
        base, properties = catalog.resolved_state(state)
    return base, dict(properties)


def _replacement(old, target, preserve):
    if not preserve:
        return target
    source_base, source_props = resolved_state(old)
    target_base, target_props = parse_state(target)
    supported = block_registry.block_properties().get(target_base, {})
    carried = {key: value for key, value in source_props.items()
               if value in supported.get(key, ())}
    return state_string(target_base, carried | target_props)


def apply_plan(volume, plan, *, max_operations=MAX_OPERATIONS, operation_observer=None):
    """Apply a bounded operation transaction to a copy and return an exact diff.

    Optional ``operation_observer(index, operation, positions, palette_ids)``
    receives every assignment after each operation, including same-value writes.
    Positions are flat ``x + z * width + y * width * length`` indices. This is
    an authentic execution trace, not a final diff. Observers must not modify the
    plan; their arrays are isolated copies. A failed transaction has no result.
    """
    prepared = prepare_plan(volume.dimensions, plan, max_operations=max_operations)
    edited = volume.clone()
    counts = []
    for operation_index, (operation, a, b) in enumerate(prepared):
        kind = operation["op"]
        # The preview uses one representative state. Fabric makes the per-block
        # random choice from the original plan during actual placement.
        target = operation["block"][0] if isinstance(operation["block"], list) else operation["block"]
        if kind == "line":
            points = np.rint(np.linspace(a, b, max(abs(y - x) for x, y in zip(a, b)) + 1)).astype(int)
            locations = (points[:, 1], points[:, 2], points[:, 0])
            before = edited.blocks[locations].copy()
            edited.blocks[locations] = edited.palette_id(target)
            changed = int(np.count_nonzero(before != edited.blocks[locations]))
            if operation_observer is not None:
                positions = np.ravel_multi_index(locations, edited.blocks.shape)
                assigned = edited.blocks[locations].copy()
        else:
            x0, y0, z0 = a
            x1, y1, z1 = b
            region = edited.blocks[y0:y1 + 1, z0:z1 + 1, x0:x1 + 1]
            before = region.copy()
            assignment_mask = None
            if kind == "replace":
                if operation_observer is not None:
                    assignment_mask = np.zeros(region.shape, dtype=bool)
                # Match against the pre-operation array, preventing chained replacements.
                for index in np.unique(before):
                    state = edited.palette[int(index)]
                    base = parse_state(state)[0]
                    matches = any(resolved_state(state) == resolved_state(match) if "[" in match else base == match for match in operation["match"])
                    if matches:
                        replacement = _replacement(state, target, operation["preserve_properties"])
                        matched = before == index
                        region[matched] = edited.palette_id(replacement)
                        if assignment_mask is not None:
                            assignment_mask |= matched
            elif kind in {"fill", "single"}:
                region[:] = edited.palette_id(target)
            else:
                yy, zz, xx = np.ogrid[:region.shape[0], :region.shape[1], :region.shape[2]]
                if kind == "hollow_box":
                    t = operation["thickness"]
                    mask = ((xx < t) | (xx >= region.shape[2] - t) | (yy < t) | (yy >= region.shape[0] - t) | (zz < t) | (zz >= region.shape[1] - t))
                elif kind in {"solid_cylinder", "hollow_cylinder"}:
                    # An elliptical cross-section inscribed in the box, extruded
                    # along axis. A hollow cylinder is an open-ended tube.
                    h, l, w = region.shape
                    offsets = {"x": (xx - (w - 1) / 2, w / 2), "y": (yy - (h - 1) / 2, h / 2),
                               "z": (zz - (l - 1) / 2, l / 2)}
                    (da, ra), (db, rb) = (offsets[name] for name in "xyz" if name != operation["axis"])
                    mask = np.broadcast_to((da / ra) ** 2 + (db / rb) ** 2 <= 1, region.shape)
                    if kind == "hollow_cylinder":
                        t = operation["thickness"]
                        mask = mask & ((da / (ra - t)) ** 2 + (db / (rb - t)) ** 2 >= 1)
                else:
                    h, l, w = region.shape
                    radii = np.array([w, h, l], dtype=float) / 2
                    dx, dy, dz = xx - (w - 1) / 2, yy - (h - 1) / 2, zz - (l - 1) / 2
                    mask = (dx / radii[0]) ** 2 + (dy / radii[1]) ** 2 + (dz / radii[2]) ** 2 <= 1
                    if kind == "hollow_ellipsoid":
                        inner = radii - operation["thickness"]
                        mask &= (dx / inner[0]) ** 2 + (dy / inner[1]) ** 2 + (dz / inner[2]) ** 2 >= 1
                region[mask] = edited.palette_id(target)
                assignment_mask = mask
            changed = int(np.count_nonzero(before != region))
            if operation_observer is not None:
                # Convert a local box's assigned positions without building a
                # full-volume coordinate grid for each operation.
                local = (np.arange(region.size, dtype=np.int64) if assignment_mask is None
                         else np.flatnonzero(assignment_mask))
                width, _, length = edited.dimensions
                box_width, box_length = region.shape[2], region.shape[1]
                positions = ((local % box_width + x0) +
                             (local // box_width % box_length + z0) * width +
                             (local // (box_width * box_length) + y0) * width * length)
                assigned = region.ravel()[local].copy()
        if operation_observer is not None:
            operation_observer(operation_index, operation, positions, assigned)
        counts.append({"op": kind, "changed_blocks": changed})
    # The source palette is a prefix because edits append without reordering it.
    changed_mask = edited.blocks != volume.blocks
    return edited, {"dimensions": list(edited.dimensions), "changed_blocks": int(changed_mask.sum()),
                    "changed_bounds": bounds_of(changed_mask),
                    "operation_counts": counts}


def prepare_plan(dimensions, plan, *, max_operations=MAX_OPERATIONS):
    """Validate operations without allocating or editing a block volume."""
    if not isinstance(plan, dict) or type(plan.get("version")) is not int or plan.get("version") != PLAN_VERSION or not isinstance(plan.get("summary"), str):
        raise VolumeError("Plan requires version 1, summary and operations")
    if set(plan) - {"version", "summary", "operations"}:
        raise VolumeError("Unknown plan fields")
    operations = plan.get("operations")
    if operations == []:
        raise VolumeError("No changes proposed: " + plan["summary"][:1000])
    if type(max_operations) is not int or not 1 <= max_operations <= MAX_OPERATIONS:
        raise VolumeError(f"Operation budget must be an integer from 1 to {MAX_OPERATIONS}")
    if not isinstance(operations, list) or not 1 <= len(operations) <= max_operations:
        raise VolumeError(f"Plan must contain 1–{max_operations} operations")
    prepared = []
    has_live_catalog = block_registry.current_catalog() is not None
    work = 0
    for i, operation in enumerate(operations):
        if not isinstance(operation, dict) or not isinstance(operation.get("op"), str) or operation.get("op") not in OPERATIONS:
            raise VolumeError(f"Operation {i}: unsupported operation")
        spec = OPERATIONS[operation["op"]]
        if set(operation) != spec.expected_fields:
            raise VolumeError(f"Operation {i}: expected fields {sorted(spec.expected_fields)}")
        targets = operation["block"] if isinstance(operation["block"], list) else [operation["block"]]
        if not 1 <= len(targets) <= MAX_BLOCK_CHOICES:
            raise VolumeError(f"Operation {i}: block list must have 1–{MAX_BLOCK_CHOICES} states")
        for target in targets:
            validate_state(target)
        anchors = spec.points
        a = coord(operation[anchors[0]], dimensions, "at/start/min")
        b = a if len(anchors) == 1 else coord(operation[anchors[1]], dimensions, "end/max")
        if spec.geometry == "box" and any(x > y for x, y in zip(a, b)):
            raise VolumeError(f"Operation {i}: min exceeds max")
        shape = [abs(y - x) + 1 for x, y in zip(a, b)]
        work += max(shape) if spec.geometry == "line" else int(np.prod(shape))
        if work > MAX_WORK:
            raise VolumeError("Plan exceeds local operation workload budget")
        problem = prepare_problem(spec, operation, shape)
        if problem:
            raise VolumeError(problem)
        for match in operation.get("match", ()):
            base, _ = parse_state(match)
            if base.startswith("minecraft:") or has_live_catalog:
                validate_state(match)
        prepared.append((operation, a, b))
    return prepared


def validate_plan(plan, dimensions, max_operations=MAX_OPERATIONS, max_plan_bytes=MAX_PLAN_BYTES):
    """Planner-facing check of a complete candidate plan; returns structured diagnostics, never raises.

    Collects up to ``MAX_PLAN_ERRORS`` per-state registry errors, then geometry errors from
    ``prepare_plan``. Both the ``validate_plan`` planner tool and the final
    submission check use it.
    """
    errors = []
    has_live_catalog = block_registry.current_catalog() is not None
    try:
        if len(json.dumps(plan, ensure_ascii=False, allow_nan=False).encode("utf-8")) > max_plan_bytes:
            raise ValueError("Plan exceeds the plan byte limit")
        problem = plan_shape_error(plan, max_operations)
        if problem:
            raise ValueError(problem)
    except (ValueError, TypeError) as exc:
        return {"valid": False, "errors": [{"field": "plan", "message": str(exc)}]}
    for index, operation in enumerate(plan["operations"]):
        targets = operation["block"] if isinstance(operation["block"], list) else [operation["block"]]
        states = [(f"block[{i}]" if isinstance(operation["block"], list) else "block", state, True)
                  for i, state in enumerate(targets)]
        states += [(f"match[{i}]", state, False) for i, state in enumerate(operation.get("match", []))]
        for field, state, placement in states:
            # Legacy requests retain syntax-only checks for modded matches; a live
            # catalog is complete, so every match must resolve against it.
            try:
                base, _ = parse_state(state)
                if placement or base.startswith("minecraft:") or has_live_catalog:
                    validate_state(state)
            except VolumeError as exc:
                errors.append({"operation": index + 1, "field": field, "message": str(exc)})
                if len(errors) >= MAX_PLAN_ERRORS:
                    return {"valid": False, "errors": errors, "truncated": True}
    if not errors and plan["operations"]:
        try:
            prepare_plan(dimensions, plan, max_operations=max_operations)
        except VolumeError as exc:
            errors.append({"field": "geometry", "message": str(exc)})
    return {"valid": not errors, "errors": errors,
            "note": "Checks IDs, states, schema, bounds and workload; not physics, support, NBT or design quality."}
