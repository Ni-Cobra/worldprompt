"""The plan contract is generated from operations.py and pinned against its pre-refactor form."""
import json

import pytest

from plan_contract import FIXTURE, capture
from worldprompt_core import agent, limits, planner_tools
from worldprompt_core.operations import FIELD_KINDS, OPERATIONS, field_schema, operation_guide, plan_schema, plan_shape_error, prepare_problem


@pytest.fixture(scope="module")
def pinned():
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def current():
    return capture()


def test_generated_schemas_match_the_pinned_capture_exactly(pinned, current):
    # Compared as serialized text, so property and required-list order (which strict
    # structured-output schemas make part of the contract) cannot drift either.
    assert current["schemas"] == pinned["schemas"]


def test_instruction_and_tool_text_match_the_pinned_capture(pinned, current):
    assert current["texts"] == pinned["texts"]
    assert current["tools"] == pinned["tools"]


def test_validation_outcomes_match_the_pinned_corpus(pinned, current):
    assert current["cases"].keys() == pinned["cases"].keys()
    different = [label for label, case in current["cases"].items() if case != pinned["cases"][label]]
    assert not different, different[:10]


def test_schema_stays_strict_mode_compatible():
    for limit in (1, 512):
        schema = plan_schema(limit)
        assert schema["additionalProperties"] is False and schema["required"] == ["version", "summary", "operations"]
        variants = schema["properties"]["operations"]["items"]["anyOf"]
        assert [v["properties"]["op"]["enum"] for v in variants] == [[name] for name in OPERATIONS]
        for variant in variants:
            assert variant["additionalProperties"] is False
            assert variant["required"] == list(variant["properties"])


def test_every_operation_spec_is_consistent():
    assert list(OPERATIONS) == ["fill", "single", "replace", "hollow_box", "solid_ellipsoid", "hollow_ellipsoid",
                                "solid_cylinder", "hollow_cylinder", "line"]
    for name, spec in OPERATIONS.items():
        assert spec.name == name and spec.guide.startswith(name + ":") and spec.guide.endswith("\n")
        assert all(f.kind in FIELD_KINDS for f in spec.fields)
        assert len(spec.field_names) == len(spec.fields), "duplicate field"
        assert {"point": 1, "line": 2, "box": 2}[spec.geometry] == len(spec.points)
        assert spec.geometry != "box" or spec.points == ("min", "max")
        assert ("thickness" in spec.field_names) == bool(spec.wall)
        assert not spec.interior or spec.wall
    assert operation_guide().count("\n") == sum(spec.guide.count("\n") for spec in OPERATIONS.values())


def test_a_new_operation_needs_only_a_spec(monkeypatch):
    from worldprompt_core import operations
    from worldprompt_core.operations import Field, OpSpec
    spec = OpSpec("pillar", "line", (Field("start", "point"), Field("end", "point"), Field("block", "block"), Field("thickness", "thickness")),
                  "pillar: paint a pillar.\n", wall="Pillar")
    monkeypatch.setitem(operations.OPERATIONS, "pillar", spec)
    operation = {"op": "pillar", "start": [0, 0, 0], "end": [0, 3, 0], "block": "minecraft:stone", "thickness": 1}
    plan = {"version": 1, "summary": "", "operations": [operation]}
    assert plan_shape_error(plan) is None
    assert plan_shape_error({**plan, "operations": [{**operation, "thickness": 0}]}) == "Operation 1 has invalid thickness"
    assert plan_shape_error({**plan, "operations": [{**operation, "extra": 1}]}) == "Operation 1 has missing or extra fields"
    assert plan_schema()["properties"]["operations"]["items"]["anyOf"][-1]["properties"]["op"]["enum"] == ["pillar"]
    assert operation_guide().endswith("pillar: paint a pillar.\n")
    assert prepare_problem(spec, {**operation, "thickness": 0}, [1, 4, 1]) == "Pillar thickness must be a positive integer"
    from worldprompt_core.core import prepare_plan
    assert prepare_plan([4, 4, 4], plan)[0][0] == operation


def test_point_schema_is_shared_with_the_planner_tools():
    tools = {tool["name"]: tool for tool in planner_tools.tool_definitions()}
    assert tools["inspect_region"]["inputSchema"]["properties"]["min"] == field_schema("point")
    assert field_schema("point") is not field_schema("point")


def test_numbers_in_the_planner_text_come_from_the_limits_module():
    assert limits.MAX_TOOL_CALLS == 32 and limits.MAX_PREVIEWS == 2 and limits.MAX_INSPECT_BLOCKS == 4096
    assert (limits.MAX_PLAN_BYTES, limits.MAX_PROMPT_BYTES) == (131_072, 64_000)
    assert limits.TERMINAL_STATUSES == {"completed", "failed"}
    assert f"at most {limits.MAX_TOOL_CALLS} tool calls per pass" in agent.INSTRUCTIONS
    assert f"{limits.MAX_INSPECT_BLOCKS} blocks\nmaximum per call" in agent.INSTRUCTIONS
    assert f"{limits.number_word(limits.MAX_PREVIEWS)} previews per pass" in agent.INSTRUCTIONS
    descriptions = " ".join(tool["description"] for tool in planner_tools.tool_definitions())
    assert f"up to {limits.MAX_INSPECT_BLOCKS} blocks" in descriptions and f"At most {limits.MAX_PREVIEWS} previews" in descriptions


def test_tool_call_budget_comes_from_limits(monkeypatch):
    monkeypatch.setattr(planner_tools, "MAX_TOOL_CALLS", 1)
    tools = planner_tools.PlannerTools({"volume": {"dimensions": [2, 2, 2]}, "limits": {"max_operations": 4}})
    tools.call("list_blocks", {})
    with pytest.raises(ValueError, match=r"budget exhausted \(1 calls per pass\)"):
        tools.call("list_blocks", {})
