"""The edit-plan contract: one spec per operation, everything else is generated from it.

``OPERATIONS`` lists every operation with its fields, their types and limits, and the
sentence the planner reads about it. These are generated from the table:

* ``plan_schema``: the JSON schema sent to the CLI as its structured-output schema,
* ``plan_shape_error``: the protocol check on a plan returned by the planner,
* ``prepare_problem``: the per-operation semantic checks ``core.prepare_plan`` runs,
* ``operation_guide``: the per-operation lines of the planner's instructions.

Adding an operation is one new ``OpSpec`` here plus its geometry in ``core.apply_plan``
(and the Fabric mod's placement). Adding a field type means one new ``FieldKind``.
This module never imports ``core`` (which imports it) and raises nothing: callers
turn the returned message into their own exception type.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from .block_registry import VERSION as MINECRAFT_VERSION
from .limits import MAX_BLOCK_CHOICES, MAX_OPERATIONS, MAX_STATE_CHARS, MAX_SUMMARY_CHARS

PLAN_VERSION = 1
AXES = ("x", "y", "z")


def _states(value: Any) -> bool:
    return isinstance(value, list) and 1 <= len(value) <= MAX_BLOCK_CHOICES and all(
        isinstance(v, str) and 1 <= len(v) <= MAX_STATE_CHARS for v in value)


def _state_schema() -> dict:
    return {"type": "string", "minLength": 1, "maxLength": MAX_STATE_CHARS}


def _state_list_schema() -> dict:
    return {"type": "array", "items": _state_schema(), "minItems": 1, "maxItems": MAX_BLOCK_CHOICES}


@dataclass(frozen=True)
class FieldKind:
    """How one kind of operation field is described and judged.

    ``rank`` orders the protocol checks (the first failing field names the error).
    ``problem`` completes "Operation N " with the protocol error message.
    """
    schema: Callable[[], dict]
    valid: Callable[[Any], bool]
    problem: Callable[[str], str]
    rank: int


FIELD_KINDS = {
    "point": FieldKind(lambda: {"type": "array", "items": {"type": "integer", "minimum": 0}, "minItems": 3, "maxItems": 3},
                       lambda v: isinstance(v, list) and len(v) == 3 and all(type(n) is int and n >= 0 for n in v),
                       lambda name: f"has invalid {name} coordinates", 1),
    # One state, or a list Fabric chooses from independently for each placed block.
    "block": FieldKind(lambda: {"anyOf": [_state_schema(), _state_list_schema()]},
                       lambda v: _states([v] if isinstance(v, str) else v),
                       lambda name: "has an invalid block", 0),
    "match": FieldKind(_state_list_schema, _states, lambda name: "has invalid replacement fields", 2),
    "flag": FieldKind(lambda: {"type": "boolean"}, lambda v: type(v) is bool,
                      lambda name: "has invalid replacement fields", 2),
    "thickness": FieldKind(lambda: {"type": "integer", "minimum": 1}, lambda v: type(v) is int and v >= 1,
                           lambda name: "has invalid thickness", 3),
    "axis": FieldKind(lambda: {"type": "string", "enum": list(AXES)}, lambda v: v in AXES,
                      lambda name: "has an invalid cylinder axis", 4),
}


@dataclass(frozen=True)
class Field:
    name: str
    kind: str


@dataclass(frozen=True)
class OpSpec:
    """One operation. ``fields`` are in schema order (anchor points, block, then extras).

    ``geometry`` is ``point`` (one anchor), ``line`` (two anchors, any order) or
    ``box`` (min/max corners). ``wall`` names a shell thickness in error messages and
    ``interior`` says what must stay larger than twice the thickness: the whole
    ``box`` or only the ``cross_section`` perpendicular to ``axis``.
    """
    name: str
    geometry: str
    fields: tuple[Field, ...]
    guide: str
    wall: str = ""
    interior: str = ""

    @property
    def points(self) -> tuple[str, ...]:
        return tuple(f.name for f in self.fields if f.kind == "point")

    @property
    def field_names(self) -> frozenset[str]:
        return frozenset(f.name for f in self.fields)

    @property
    def expected_fields(self) -> frozenset[str]:
        return self.field_names | {"op"}

    def schema(self) -> dict:
        properties = {"op": {"type": "string", "enum": [self.name]},
                      **{f.name: FIELD_KINDS[f.kind].schema() for f in self.fields}}
        return {"type": "object", "properties": properties, "required": list(properties), "additionalProperties": False}


def _fields(points: tuple[str, ...], *extras: tuple[str, str]) -> tuple[Field, ...]:
    return (*(Field(name, "point") for name in points), Field("block", "block"),
            *(Field(name, kind) for name, kind in extras))


_BOX, _THICKNESS, _AXIS = ("min", "max"), ("thickness", "thickness"), ("axis", "axis")

# The planner reads these lines verbatim, in this order, after the block-list paragraph.
OPERATIONS: dict[str, OpSpec] = {spec.name: spec for spec in (
    OpSpec("fill", "box", _fields(_BOX),
           "fill: fill the inclusive min..max cuboid with block (air carves it).\n"),
    OpSpec("single", "point", _fields(("at",)),
           "single: place one block at the local coordinate at.\n"),
    OpSpec("replace", "box", _fields(_BOX, ("match", "match"), ("preserve_properties", "flag")),
           "replace: replace states matching a base block ID or exact full state within bounds.\n"
           "preserve_properties=true carries only source properties whose names AND values\n"
           "are supported by the target block (e.g. log axis survives replacement with a\n"
           "crimson stem). Unsupported properties are dropped; explicit target properties\n"
           "win. Use false to use only the specified target state. Compatibility comes from\n"
           f"the vanilla Minecraft {MINECRAFT_VERSION} registry; unknown/modded targets inherit no properties.\n"),
    OpSpec("hollow_box", "box", _fields(_BOX, _THICKNESS),
           "hollow_box: paint the shell of thickness blocks; interior is unchanged. To clear\n"
           "an interior, use an air fill first. Ensure thickness fits the box dimensions.\n",
           wall="Shell"),
    OpSpec("solid_ellipsoid", "box", _fields(_BOX),
           "solid_ellipsoid: paint a solid ellipsoid inscribed in min..max.\n"),
    OpSpec("hollow_ellipsoid", "box", _fields(_BOX, _THICKNESS),
           "hollow_ellipsoid: paint the outer ellipsoid minus an inner ellipsoid whose\n"
           "radii are smaller by thickness blocks; interior is unchanged. Each box\n"
           "dimension must exceed twice the thickness.\n",
           wall="Ellipsoid shell", interior="box"),
    OpSpec("solid_cylinder", "box", _fields(_BOX, _AXIS),
           "solid_cylinder: paint a cylinder along axis (x, y or z) whose elliptical\n"
           "cross-section is inscribed in min..max; it spans the whole box along axis.\n"),
    OpSpec("hollow_cylinder", "box", _fields(_BOX, _AXIS, _THICKNESS),
           "hollow_cylinder: an open-ended tube: the solid_cylinder minus an inner one whose\n"
           "cross-section radii are smaller by thickness blocks; interior and ends are\n"
           "unchanged. Both cross-section dimensions must exceed twice the thickness. Add a\n"
           "fill or solid_cylinder for a floor or cap, and an air fill to clear an interior.\n",
           wall="Cylinder wall", interior="cross_section"),
    OpSpec("line", "line", _fields(("start", "end")),
           "line: paint a one-block-thick line from start to end.\n"),
)}


def block_choices_guide() -> str:
    """The instruction paragraph on block lists, which every operation's block field accepts."""
    return (f"Every block field may be a block state or a nonempty list of up to {MAX_BLOCK_CHOICES} block\n"
            "states. Fabric chooses independently and uniformly from a list for each placed\n"
            "block. Repeating an entry increases its chance. Core previews use the first\n"
            "listed state as a representative; the final pattern is chosen in Minecraft.\n")


def operation_guide() -> str:
    """The per-operation lines of the planner's instructions."""
    return "".join(spec.guide for spec in OPERATIONS.values())


def field_schema(kind: str) -> dict:
    """A fresh JSON schema for one field kind (the planner tools reuse ``point``)."""
    return FIELD_KINDS[kind].schema()


def plan_schema(max_operations: int = MAX_OPERATIONS) -> dict[str, Any]:
    return {"type": "object", "properties": {
        "version": {"type": "integer", "enum": [PLAN_VERSION]},
        "summary": {"type": "string", "maxLength": MAX_SUMMARY_CHARS},
        "operations": {"type": "array", "maxItems": max_operations,
                       "items": {"anyOf": [spec.schema() for spec in OPERATIONS.values()]}},
    }, "required": ["version", "summary", "operations"], "additionalProperties": False}


def plan_shape_error(plan: Any, max_operations: int = MAX_OPERATIONS) -> str | None:
    """Why ``plan`` breaks the planner protocol, or None. Engine validation checks semantics."""
    if not isinstance(plan, dict) or set(plan) != {"version", "summary", "operations"}:
        return "The planner returned an invalid plan object"
    if (type(plan["version"]) is not int or plan["version"] != PLAN_VERSION or not isinstance(plan["summary"], str)
            or len(plan["summary"]) > MAX_SUMMARY_CHARS):
        return "The planner returned an invalid plan version or summary"
    operations = plan["operations"]
    if not isinstance(operations, list) or len(operations) > max_operations:
        return f"The planner exceeded the {max_operations}-operation limit"
    for index, operation in enumerate(operations):
        if not isinstance(operation, dict):
            return f"Operation {index + 1} is not an object"
        name = operation.get("op")
        if not isinstance(name, str) or name not in OPERATIONS:
            return f"Operation {index + 1} has an unsupported operation"
        spec = OPERATIONS[name]
        if set(operation) != spec.expected_fields:
            return f"Operation {index + 1} has missing or extra fields"
        for field in sorted(spec.fields, key=lambda f: FIELD_KINDS[f.kind].rank):
            kind = FIELD_KINDS[field.kind]
            if not kind.valid(operation[field.name]):
                return f"Operation {index + 1} {kind.problem(field.name)}"
    return None


def prepare_problem(spec: OpSpec, operation: dict, shape: list[int]) -> str | None:
    """Semantic problems with one operation's extra fields, given its box ``shape`` (x, y, z)."""
    if "match" in spec.field_names and (
            type(operation["preserve_properties"]) is not bool or not isinstance(operation["match"], list)
            or not 1 <= len(operation["match"]) <= MAX_BLOCK_CHOICES):
        return "Replace requires match states and boolean preserve_properties"
    if "axis" in spec.field_names and (not isinstance(operation["axis"], str) or operation["axis"] not in AXES):
        return "Cylinder axis must be x, y or z"
    if "thickness" in spec.field_names:
        thickness = operation["thickness"]
        if type(thickness) is not int or thickness < 1:
            return f"{spec.wall} thickness must be a positive integer"
        if spec.interior:
            sizes = shape if spec.interior == "box" else [size for size, axis in zip(shape, "xyz") if axis != operation["axis"]]
            if any(size <= 2 * thickness for size in sizes):
                return f"{spec.wall} thickness leaves no interior"
    return None
