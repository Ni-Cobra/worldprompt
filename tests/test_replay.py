"""Replay preserves authentic operation writes and the engine's static semantics."""
import copy
import json

import numpy as np
import pytest

from worldprompt_core.core import BlockVolume, apply_plan
from worldprompt_core.replay import compile_replay
from replay_helpers import operation_runs


def volume():
    source = BlockVolume.empty((9, 8, 10))
    source.blocks[0] = source.palette_id("minecraft:stone")
    source.blocks[1, :, ::2] = source.palette_id("minecraft:oak_stairs[half=bottom,facing=north]")
    source.blocks[1, :, 1::2] = source.palette_id("minecraft:oak_stairs[facing=east,half=top]")
    return source


def make_plan(operations):
    return {"version": 1, "summary": "Exercise authentic operations.", "operations": operations}


@pytest.mark.parametrize("ellipsoid", [
    {"op": "solid_ellipsoid"},
    {"op": "hollow_ellipsoid", "thickness": 1},
    {"op": "hollow_ellipsoid", "thickness": 2},
    {"op": "solid_cylinder", "axis": "y"},
    {"op": "hollow_cylinder", "axis": "z", "thickness": 2},
])
def test_all_shapes_and_directional_replacement_match_every_engine_prefix(tmp_path, ellipsoid):
    source = volume()
    plan = make_plan([
        {"op": "replace", "min": [0, 1, 0], "max": [8, 1, 9],
         "block": "minecraft:birch_stairs", "match": ["minecraft:oak_stairs"], "preserve_properties": True},
        {"op": "fill", "min": [1, 2, 1], "max": [7, 6, 8], "block": "minecraft:stone"},
        {"op": "fill", "min": [2, 3, 2], "max": [6, 5, 7], "block": "minecraft:air"},
        {"op": "hollow_box", "min": [0, 0, 0], "max": [8, 7, 9],
         "block": "minecraft:glass", "thickness": 1},
        {**ellipsoid, "min": [1, 2, 1], "max": [7, 6, 8], "block": "minecraft:oak_leaves"},
        {"op": "line", "start": [8, 7, 9], "end": [0, 1, 0], "block": "minecraft:oak_log[axis=y]"},
        {"op": "replace", "min": [0, 1, 0], "max": [8, 1, 9],
         "block": "minecraft:spruce_stairs[facing=south]",
         "match": ["minecraft:birch_stairs[facing=east,half=top]"], "preserve_properties": True},
        {"op": "replace", "min": [0, 0, 0], "max": [8, 7, 9],
         "block": "minecraft:gold_block", "match": ["minecraft:diamond_block"], "preserve_properties": False},
    ])
    unchanged = copy.deepcopy(plan)
    destination = tmp_path / "replay"
    manifest = compile_replay(source, plan, destination).manifest
    assert plan == unchanged
    assert json.loads((destination / "plan.json").read_text()) == plan
    assert manifest["dimensions"] == list(source.dimensions)
    reconstructed = source.blocks.copy().ravel()
    for index in range(len(plan["operations"])):
        assigned_count = 0
        for start, length, palette_id in operation_runs(destination, index):
            reconstructed[start:start + length] = palette_id
            assigned_count += length
        expected, _ = apply_plan(source, make_plan(plan["operations"][:index + 1]))
        assert manifest["palette"][:len(expected.palette)] == expected.palette
        np.testing.assert_array_equal(reconstructed, expected.blocks.ravel())
        assert assigned_count == manifest["operations"][index]["writes"]
    assert manifest["operations"][-1]["writes"] == 0


def test_same_value_writes_and_intermediate_carving_survive_zero_final_diff(tmp_path):
    source = BlockVolume.empty((3, 2, 2))
    plan = make_plan([
        {"op": "fill", "min": [0, 0, 0], "max": [2, 1, 1], "block": "minecraft:air"},
        {"op": "fill", "min": [1, 1, 1], "max": [1, 1, 1], "block": "minecraft:stone"},
        {"op": "fill", "min": [1, 1, 1], "max": [1, 1, 1], "block": "minecraft:air"}])
    _, report = apply_plan(source, plan)
    assert report["changed_blocks"] == 0
    manifest = compile_replay(source, plan, tmp_path / "replay").manifest
    assert [op["writes"] for op in manifest["operations"]] == [12, 1, 1]
    assert operation_runs(tmp_path / "replay", 0) == [[0, 12, 0]]
    assert operation_runs(tmp_path / "replay", 1) == [[10, 1, 1]]
    assert operation_runs(tmp_path / "replay", 2) == [[10, 1, 0]]


def test_replay_preserves_block_choices_and_single_position_for_fabric(tmp_path):
    source = BlockVolume.empty((3, 1, 1))
    plan = make_plan([
        {"op": "fill", "min": [0, 0, 0], "max": [2, 0, 0],
         "block": ["minecraft:stone", "minecraft:dirt"]},
        {"op": "single", "at": [1, 0, 0], "block": "minecraft:gold_block"},
    ])
    manifest = compile_replay(source, plan, tmp_path / "replay").manifest
    assert json.loads((tmp_path / "replay" / "plan.json").read_text()) == plan
    assert operation_runs(tmp_path / "replay", 0) == [[0, 3, 1]]
    assert operation_runs(tmp_path / "replay", 1) == [[1, 1, 2]]
    assert manifest["operations"][0]["writes"] == 3


def test_replace_after_random_choice_delegates_matching_to_fabric(tmp_path):
    source = BlockVolume.empty((3, 1, 1))
    plan = make_plan([
        {"op": "fill", "min": [0, 0, 0], "max": [2, 0, 0],
         "block": ["minecraft:stone", "minecraft:dirt"]},
        {"op": "replace", "min": [0, 0, 0], "max": [2, 0, 0],
         "block": "minecraft:cobblestone", "match": ["minecraft:dirt"],
         "preserve_properties": False},
    ])
    manifest = compile_replay(source, plan, tmp_path / "replay").manifest
    assert manifest["operations"][1]["dynamic_match"]
    assert operation_runs(tmp_path / "replay", 1) == [[0, 3, 0]]


def test_observer_arrays_cannot_change_the_engine_result():
    source = volume()
    plan = make_plan([{"op": "fill", "min": [1, 1, 1], "max": [5, 5, 5], "block": "minecraft:stone"}])
    expected, expected_report = apply_plan(source, plan)
    def observe(index, operation, positions, palette_ids):
        positions[:] = 0
        palette_ids[:] = 0
    actual, actual_report = apply_plan(source, plan, operation_observer=observe)
    np.testing.assert_array_equal(actual.blocks, expected.blocks)
    assert actual_report == expected_report


def test_invalid_plan_cannot_publish_replay_manifest(tmp_path):
    source = volume()
    plan = make_plan([{"op": "fill", "min": [0, 0, 0], "max": [999, 0, 0], "block": "minecraft:stone"}])
    with pytest.raises(ValueError):
        compile_replay(source, plan, tmp_path / "replay")
    assert not (tmp_path / "replay" / "manifest.json").exists()


def test_replay_records_replacement_of_a_prior_implicit_default_state(tmp_path):
    source = BlockVolume.empty((1, 1, 1))
    plan = make_plan([
        {"op": "single", "at": [0, 0, 0], "block": "minecraft:oak_log"},
        {"op": "replace", "min": [0, 0, 0], "max": [0, 0, 0],
         "match": ["minecraft:oak_log[axis=y]"], "block": "minecraft:gold_block",
         "preserve_properties": False},
    ])
    manifest = compile_replay(source, plan, tmp_path / "replay").manifest
    assert manifest["operations"][1]["writes"] == 1
    assert operation_runs(tmp_path / "replay", 1) == [[0, 1, manifest["palette"].index("minecraft:gold_block")]]


def test_compile_replay_returns_the_one_application_it_ran(tmp_path):
    source = volume()
    plan = make_plan([
        {"op": "fill", "min": [0, 0, 0], "max": [3, 3, 3], "block": "minecraft:glass"},
        {"op": "replace", "min": [0, 0, 0], "max": [8, 7, 9], "block": "minecraft:gold_block",
         "match": ["minecraft:stone"], "preserve_properties": False}])
    expected, expected_report = apply_plan(source, plan)
    result = compile_replay(source, plan, tmp_path / "replay")
    manifest, edited, report = result
    assert manifest is result.manifest and report == expected_report
    np.testing.assert_array_equal(edited.blocks, expected.blocks)
    assert edited.palette == expected.palette == manifest["palette"]
