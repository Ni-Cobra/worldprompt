"""Deterministic capture of the plan contract: schema, prompt text, validation outcomes.

``capture()`` records what the planner sees (the JSON schema, the instruction text, the
tool definitions) and how every layer judges a large corpus of valid and broken plans.
``tests/fixtures/plan_contract.json`` holds the capture taken before the contract moved
into ``operations.py``; ``test_operations`` compares it to a fresh capture, so any drift
in the generated contract (including key order and error messages) fails a test.
Regenerate it deliberately with ``python -m tests.plan_contract`` when the contract changes.
"""
from __future__ import annotations

import copy
import json
import sys
from pathlib import Path

FIXTURE = Path(__file__).parent / "fixtures" / "plan_contract.json"
DIMENSIONS = [8, 8, 8]
STONE = "minecraft:stone"

BASE_OPS = {
    "fill": {"op": "fill", "min": [0, 0, 0], "max": [2, 2, 2], "block": STONE},
    "single": {"op": "single", "at": [1, 1, 1], "block": STONE},
    "replace": {"op": "replace", "min": [0, 0, 0], "max": [3, 3, 3], "block": "minecraft:oak_log",
                "match": [STONE], "preserve_properties": True},
    "hollow_box": {"op": "hollow_box", "min": [0, 0, 0], "max": [4, 4, 4], "block": STONE, "thickness": 1},
    "solid_ellipsoid": {"op": "solid_ellipsoid", "min": [0, 0, 0], "max": [4, 4, 4], "block": STONE},
    "hollow_ellipsoid": {"op": "hollow_ellipsoid", "min": [0, 0, 0], "max": [6, 6, 6], "block": STONE, "thickness": 1},
    "solid_cylinder": {"op": "solid_cylinder", "min": [0, 0, 0], "max": [4, 4, 4], "block": STONE, "axis": "y"},
    "hollow_cylinder": {"op": "hollow_cylinder", "min": [0, 0, 0], "max": [6, 6, 6], "block": STONE, "axis": "z", "thickness": 1},
    "line": {"op": "line", "start": [0, 0, 0], "end": [5, 3, 2], "block": STONE},
}

BAD_VALUES = [None, True, 0, -1, 1, 1.5, "", "x", "w", "X", [], {}, [1, 2], [1, 2, 3, 4], [-1, 0, 0], [0, 0, 1.0],
              [True, 0, 0], ["a", "b", "c"], [8, 0, 0], [0, 8, 0], [0, 0, 8], [STONE], [STONE, 5], ["a" * 513],
              ["minecraft:nonexistent_block"], "minecraft:nonexistent_block", "a" * 513, [STONE] * 256, [STONE] * 257,
              "minecraft:oak_log[axis=x]", "minecraft:oak_log[axis=q]", "stone", 2, 3, 4, 100]


def plan(*operations, summary="ok"):
    return {"version": 1, "summary": summary, "operations": list(operations)}


def corpus():
    cases = []

    def add(label, value):
        cases.append((label, value))

    for name, base in BASE_OPS.items():
        add(f"{name}/base", plan(base))
        for key in base:
            variant = copy.deepcopy(base)
            del variant[key]
            add(f"{name}/missing {key}", plan(variant))
            for index, bad in enumerate(BAD_VALUES):
                variant = copy.deepcopy(base)
                variant[key] = copy.deepcopy(bad)
                add(f"{name}/{key}={index}", plan(variant))
        add(f"{name}/extra", plan({**base, "extra": 1}))
        add(f"{name}/wrong op", plan({**base, "op": "ellipsoid"}))
        add(f"{name}/op list", plan({**base, "op": []}))
        # Several simultaneous problems fix which error is reported first.
        add(f"{name}/block+thickness+axis", plan({**base, "block": [], "thickness": 0, "axis": "w"}))
        add(f"{name}/thickness+axis", plan({**base, "thickness": 0, "axis": "w"}))
        add(f"{name}/block list", plan({**base, "block": [STONE, "minecraft:dirt"]}))
        add(f"{name}/two", plan(base, base))
    for label, extras in {
        "hollow_box/thick": ({"thickness": 3}, "hollow_box"), "hollow_box/huge": ({"thickness": 50}, "hollow_box"),
        "hollow_ellipsoid/tight": ({"thickness": 3}, "hollow_ellipsoid"), "hollow_ellipsoid/edge": ({"thickness": 2}, "hollow_ellipsoid"),
        "hollow_cylinder/tight": ({"thickness": 3}, "hollow_cylinder"), "hollow_cylinder/edge": ({"thickness": 2}, "hollow_cylinder"),
        "hollow_cylinder/axis x": ({"axis": "x", "thickness": 3}, "hollow_cylinder"),
        "fill/min>max": ({"min": [3, 0, 0], "max": [1, 0, 0]}, "fill"),
        "fill/out of bounds": ({"max": [8, 0, 0]}, "fill"),
        "fill/point bounds": ({"min": [7, 7, 7], "max": [7, 7, 7]}, "fill"),
        "line/reversed": ({"start": [5, 3, 2], "end": [0, 0, 0]}, "line"),
        "line/out of bounds": ({"end": [0, 0, 9]}, "line"),
        "single/out of bounds": ({"at": [0, 8, 0]}, "single"),
        "replace/modded match": ({"match": ["mod:thing", "minecraft:dirt[snowy=false]"]}, "replace"),
        "replace/unknown vanilla match": ({"match": ["minecraft:nonexistent_block"]}, "replace"),
        "replace/many matches": ({"match": [STONE] * 256}, "replace"),
        "replace/too many matches": ({"match": [STONE] * 257}, "replace"),
        "replace/empty match": ({"match": []}, "replace"),
        "replace/preserve false": ({"preserve_properties": False}, "replace"),
    }.items():
        override, kind = extras
        add(label, plan({**BASE_OPS[kind], **override}))
    huge = {"op": "fill", "min": [0, 0, 0], "max": [7, 7, 7], "block": STONE}
    add("plan/workload", plan(*[huge] * 200))  # 200 * 512 blocks: within the work budget
    add("plan/ok", plan(*BASE_OPS.values()))
    add("plan/empty", plan(summary="Cannot fit"))
    add("plan/empty long summary", plan(summary="x" * 3000))
    add("plan/not dict", [])
    add("plan/none", None)
    add("plan/missing summary", {"version": 1, "operations": []})
    add("plan/extra key", {**plan(BASE_OPS["fill"]), "extra": 1})
    for index, bad in enumerate([2, True, "1", 1.0, None, 0]):
        add(f"plan/version={index}", {**plan(BASE_OPS["fill"]), "version": bad})
    for index, bad in enumerate([1, None, "x" * 4000, "x" * 4001, ["a"]]):
        add(f"plan/summary={index}", {**plan(BASE_OPS["fill"]), "summary": bad})
    for index, bad in enumerate([None, {}, "fill", 5]):
        add(f"plan/operations={index}", {**plan(BASE_OPS["fill"]), "operations": bad})
    add("plan/ops not objects", plan("fill", 3))
    add("plan/ops many", plan(*[BASE_OPS["single"]] * 513))
    add("plan/ops 512", plan(*[BASE_OPS["single"]] * 512))
    add("plan/ops 3 of 2", plan(*[BASE_OPS["single"]] * 3))
    return cases


def dump(data):
    """JSON with one corpus case per line: a readable diff and a compact file."""
    head = {key: value for key, value in data.items() if key != "cases"}
    lines = [json.dumps(head, sort_keys=True)[:-1] + ', "cases": {']
    lines += [json.dumps(label) + ": " + json.dumps(case, sort_keys=True) + ("," if i < len(data["cases"]) - 1 else "")
              for i, (label, case) in enumerate(sorted(data["cases"].items()))]
    return "\n".join(lines + ["}}", ""])


def outcome(function, *args, **kwargs):
    try:
        function(*args, **kwargs)
        return None
    except Exception as exc:  # noqa: BLE001 - the capture records every failure mode
        return f"{type(exc).__name__}: {exc}"


def capture():
    from worldprompt_core import agent, core, planner_tools
    schemas = {str(limit): json.dumps(agent.plan_schema(limit)) for limit in (512, 64, 1)}
    schemas["default"] = json.dumps(agent.plan_schema())
    texts = {"INSTRUCTIONS": agent.INSTRUCTIONS, "HIGH_INSTRUCTIONS": agent.HIGH_INSTRUCTIONS,
             "REVIEW_INSTRUCTIONS": agent.REVIEW_INSTRUCTIONS, "LECTERN_INSTRUCTIONS": agent.LECTERN_INSTRUCTIONS}
    cases = {}
    for label, value in corpus():
        limit = 2 if label == "plan/ops 3 of 2" else 512
        cases[label] = {
            "shape": outcome(agent.validate_plan_shape, copy.deepcopy(value), limit),
            "prepare": outcome(core.prepare_plan, DIMENSIONS, copy.deepcopy(value), max_operations=limit),
            "registry": core.validate_plan(copy.deepcopy(value), DIMENSIONS, limit),
        }
        if cases[label]["shape"] is not None:
            del cases[label]["registry"]  # Just the shape message, wrapped.
    return {"schemas": schemas, "texts": texts, "tools": json.dumps(planner_tools.tool_definitions()),
            "cases": cases}


if __name__ == "__main__":
    root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(root / "src"))
    FIXTURE.parent.mkdir(exist_ok=True)
    FIXTURE.write_text(dump(capture()), encoding="utf-8")
    print("wrote", FIXTURE)
