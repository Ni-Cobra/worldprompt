"""Measured landscape evidence, intent-neutral review, and viewpoint propagation."""
import json
from types import SimpleNamespace

import numpy as np
import pytest
from PIL import Image

from worldprompt_core.agent import HIGH_INSTRUCTIONS, INSTRUCTIONS, REVIEW_INSTRUCTIONS
from worldprompt_core.core import BlockVolume
from worldprompt_core.pipeline import agent_config_for, plan_volume
from worldprompt_core.spatial import fit_diagnostics, terrain_context


def make_landscape():
    """Small repeatable live-evaluation source: shoreline, hillside, ridge, trees."""
    source = BlockVolume.empty((33, 16, 33))
    stone, grass, sand, water, log, leaves = [source.palette_id("minecraft:" + name)
        for name in ("stone", "grass_block", "sand", "water", "oak_log[axis=y]", "oak_leaves[persistent=true]")]
    for z in range(33):
        for x in range(33):
            y = 1 if x < 6 else 2 + x // 6 + (2 if 8 <= z <= 16 else 0)
            source.blocks[:y, z, x] = stone
            source.blocks[y, z, x] = sand if x < 6 else grass
            if x < 6:
                source.blocks[y + 1:4, z, x] = water
    for x, z in ((10, 6), (25, 23)):
        y = 2 + x // 6
        source.blocks[y + 1:y + 4, z, x] = log
        source.blocks[y + 4, z - 1:z + 2, x - 1:x + 2] = leaves
    return source


# Reusable with the configured real planner; tests below use local fixtures only.
QUALITY_PROMPTS = {
    "landscape": "Soften the straight exposed ridge edges and enrich this hillside and shoreline with natural transitions of rock, moss and vegetation. Keep the overall elevations, water and recognizable terrain; do not add a building.",
    "distributed": "Scatter small mossy boulders and varied flower patches across suitable parts of the whole hillside and shoreline. Use separated clusters and open space, follow local ground heights, and preserve the terrain and water. Do not concentrate them around one central feature.",
    "focused": "Build one small open wooden pavilion on a suitable dry part of this hillside. Give it terrain-contacting supports and a walkable approach. Keep it a single focused build and preserve the rest of the terrain.",
}


def test_landscape_survey_distinguishes_landform_water_and_tree_canopy():
    source = make_landscape()
    context = terrain_context(source)
    samples = {(row[0], row[1]): dict(zip(context["column_fields"], row)) for row in context["columns"]}
    assert samples[0, 0]["highest_terrain_candidate_y"] == 1
    assert samples[0, 0]["highest_water_y"] == 3
    assert samples[0, 0]["highest_non_air_y"] == 3
    assert samples[0, 0]["strict_air_cells_above_terrain"] == 0
    assert samples[32, 12]["highest_terrain_candidate_y"] == 9
    assert samples[32, 12]["highest_water_y"] == -1
    # The 24,24 sample falls under canopy near the second tree, not its trunk.
    canopy = samples[24, 24]
    assert canopy["highest_non_air_y"] == 10
    assert canopy["highest_terrain_candidate_y"] == 6
    assert context["materials"][canopy["top_material_index"]] == "minecraft:oak_leaves"
    assert context["materials"][canopy["terrain_material_index"]] == "minecraft:grass_block"
    assert canopy["strict_air_cells_above_terrain"] == 3
    tiles = [dict(zip(context["tile_fields"], row)) for row in context["tiles"]]
    assert sum(tile["columns"] for tile in tiles) == 33 * 33
    assert sum(tile["exposed_water_columns"] for tile in tiles) == 6 * 33
    assert sum(tile["columns_with_neighbor_terrain_step_over_one"] for tile in tiles) > 0
    assert len(json.dumps(context).encode()) < 14000


def test_terrain_height_is_not_a_claim_of_solid_ground_or_a_biome():
    source = BlockVolume.empty((3, 10, 3))
    source.blocks[0] = source.palette_id("minecraft:stone")
    source.blocks[6] = source.palette_id("minecraft:stone")
    source.blocks[8, 1, 1] = source.palette_id("minecraft:oak_log[axis=y]")
    context = terrain_context(source)
    middle = dict(zip(context["column_fields"], context["columns"][4]))
    assert middle["highest_terrain_candidate_y"] == 6
    assert middle["highest_non_air_y"] == 8
    assert middle["strict_air_cells_above_terrain"] == 1
    assert "caves/overhangs" in " ".join(context["caveats"])
    assert "No biome data" in " ".join(context["caveats"])


@pytest.mark.parametrize("dimensions", [(1, 1, 1), (32767, 1, 1), (1, 32767, 1), (1, 1, 32767), (199, 101, 199)])
def test_landscape_context_bounds_and_empty_columns(dimensions):
    context = terrain_context(BlockVolume.empty(dimensions))
    assert len(context["columns"]) <= 81
    assert len(context["tiles"]) <= 16
    assert len(context["materials"]) <= 16
    assert len(json.dumps(context).encode()) < 14000
    assert all(column[2:8] == [-1] * 6 for column in context["columns"])
    assert all(column[-1] is None for column in context["columns"])
    assert sum(tile[4] for tile in context["tiles"]) == dimensions[0] * dimensions[2]


def test_distribution_distinguishes_same_size_concentrated_and_scattered_edits():
    source = BlockVolume.empty((16, 10, 16))
    source.blocks[0] = source.palette_id("minecraft:grass_block")
    compact, scattered = source.clone(), source.clone()
    compact.blocks[1, 6:10, 6:10] = compact.palette_id("minecraft:mossy_cobblestone")
    for x in (1, 5, 9, 13):
        for z in (1, 5, 9, 13):
            scattered.blocks[1, z, x] = scattered.palette_id("minecraft:mossy_cobblestone")
    reports = [fit_diagnostics(source, edited) for edited in (compact, scattered)]
    assert [report["changed_voxels"] for report in reports] == [16, 16]
    assert [report["spatial_distribution"]["tiles_with_changes"] for report in reports] == [4, 16]
    for report in reports:
        assert report["terrain_surface"]["source_surface_blocks_changed"] == 0
        assert report["added_support_without_support_immediately_below"] == 0
        assert "sparse or unchanged tiles can be intentional" in report["spatial_distribution"]["interpretation"]


def test_landform_diagnostics_count_surface_removal_and_restyle_separately():
    source = BlockVolume.empty((8, 10, 8))
    source.blocks[:4] = source.palette_id("minecraft:stone")
    source.blocks[4] = source.palette_id("minecraft:grass_block")
    edited = source.clone()
    edited.blocks[3:5, :2, :2] = 0
    edited.blocks[4, 5:7, 5:7] = edited.palette_id("minecraft:moss_block")
    report = fit_diagnostics(source, edited)["terrain_surface"]
    assert report["source_surface_columns"] == 64
    assert report["source_surface_blocks_changed"] == 8
    assert report["source_surface_blocks_became_air"] == 4
    assert report["columns_with_lower_highest_terrain_candidate"] == 4
    assert report["columns_with_higher_highest_terrain_candidate"] == 0


@pytest.mark.parametrize("effort,expected_phases", [("normal", ["normal"]), ("high", ["draft", "review"])])
@pytest.mark.parametrize("with_viewpoint", [False, True])
def test_every_pass_gets_terrain_and_optional_submission_viewpoint(tmp_path, effort, expected_phases, with_viewpoint):
    source = BlockVolume.empty((9, 8, 9))
    source.blocks[0] = source.palette_id("minecraft:grass_block")
    viewpoint = {"position_local": [-5.25, 2.0, 3.5], "eye_position_local": [-5.25, 3.62, 3.5],
                 "look_direction": [1.0, 0.0, 0.0], "yaw_degrees": -90.0, "pitch_degrees": 0.0,
                 "world_position": [100.25, 70.0, -40.5], "selection_origin": [105, 68, -44],
                 "dimension": "minecraft:overworld"}
    calls = []
    plan = {"version": 1, "summary": "A grounded marker.", "operations": [
        {"op": "fill", "min": [2, 1, 2], "max": [2, 1, 2], "block": "minecraft:mossy_cobblestone"}]}

    def planner(summary, prompt, directory, config, preview_paths=(), *, phase="normal", context=None):
        directory.mkdir()
        calls.append(phase)
        assert summary["terrain_context"]["columns"]
        assert config.reasoning_effort == "medium"
        if with_viewpoint:
            assert context["viewpoint_context"] == viewpoint
        else:
            assert "viewpoint_context" not in (context or {})
        if phase == "review":
            assert context["fit_diagnostics"]["spatial_distribution"]["changed_columns"] == 1
            assert context["fit_diagnostics"]["terrain_surface"]["source_surface_blocks_changed"] == 0
            assert [path.name for path in preview_paths] == ["source_overview.png", "source_atlas.png", "draft_overview.png", "draft_atlas.png"]
            for overview in (preview_paths[0], preview_paths[2]):
                with Image.open(overview) as image:
                    assert image.size == (1536, 832)
                    image.verify()
                assert overview.stat().st_size < 4_000_000
        return SimpleNamespace(plan=plan, usage={"input_tokens": 100, "output_tokens": 20},
                               elapsed_seconds=1.0, model="fixture")

    result = plan_volume(source, "Place one marker", tmp_path / "call", tmp_path / "previews",
                         agent_config_for({}, effort), effort, planner=planner,
                         viewpoint_context=viewpoint if with_viewpoint else None)
    assert calls == expected_phases
    assert result.plan == plan
    assert result.edited.dimensions == source.dimensions
    assert result.usage["input_tokens"] == 100 * len(expected_phases)
    assert np.count_nonzero(source.blocks) == 81


def test_prompt_scope_keeps_focused_and_distributed_tasks_valid():
    assert "For a single\nbuild, keep its focus" in INSTRUCTIONS
    assert "landform itself is part of the design" in INSTRUCTIONS
    assert "separated parts" in INSTRUCTIONS
    assert "weak cue" in INSTRUCTIONS
    assert "Choose one workable site" not in HIGH_INSTRUCTIONS
    assert "decorative work can itself be the main task" in HIGH_INSTRUCTIONS
    assert "not whether a design is good" in REVIEW_INSTRUCTIONS
