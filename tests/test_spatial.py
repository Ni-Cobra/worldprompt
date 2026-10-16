import json

import numpy as np
import pytest
from PIL import Image

from worldprompt_core.core import BlockVolume, VolumeError
from worldprompt_core.spatial import fit_diagnostics, render_spatial_atlas, spatial_context


def put(volume, block, index):
    volume.blocks[index] = volume.palette_id(block)


def test_sloped_terrain_reports_exact_surface_and_tile_ranges():
    source = BlockVolume.empty((9, 12, 9))
    for x in range(9):
        put(source, "minecraft:stone", (slice(0, x + 1), slice(None), x))
    context = spatial_context(source)
    assert len(context["columns"]) == 81
    for x, z, top, support, runs in context["columns"]:
        assert top == support == x
        assert runs[0] == [x + 1, 11, x, None, True]
    for x0, z0, x1, z1, occupied, supports, minimum, maximum, floors, floor_columns in context["tiles"]:
        assert minimum == x0
        assert maximum == x1
        assert floors == floor_columns == occupied == supports == (x1 - x0 + 1) * (z1 - z0 + 1)
    assert sum(row[2] for row in context["vertical_profile"]) == 405


def test_cave_air_runs_distinguish_floor_ceiling_and_headroom():
    source = BlockVolume.empty((3, 12, 3))
    put(source, "minecraft:stone", (slice(None), slice(None), slice(None)))
    put(source, "minecraft:cave_air", (slice(3, 8), slice(None), slice(None)))
    put(source, "minecraft:air", (slice(10, 12), slice(None), slice(None)))
    context = spatial_context(source)
    x, z, top, support, runs = context["columns"][4]
    assert (x, z, top, support) == (1, 1, 9, 9)
    assert runs == [[3, 7, 2, 8, True], [10, 11, 9, None, True]]
    assert sum(tile[8] for tile in context["tiles"]) == 18


def test_liquids_plants_leaves_partial_and_unknown_blocks_are_not_ground():
    source = BlockVolume.empty((5, 9, 1))
    put(source, "minecraft:stone", (0, slice(None), slice(None)))
    for x, block in enumerate(("minecraft:water", "minecraft:oak_leaves", "minecraft:poppy",
                               "minecraft:oak_stairs[facing=east,half=bottom]", "custom:stone")):
        put(source, block, (slice(1, 5), 0, x))
    context = spatial_context(source)
    assert all(column[2:4] == [4, 0] for column in context["columns"])
    assert all(column[4][0] == [5, 8, 4, None, False] for column in context["columns"])
    assert sum(tile[8] for tile in context["tiles"]) == 0


@pytest.mark.parametrize("block", ["minecraft:torch", "minecraft:lantern", "minecraft:candle"])
def test_ordinary_light_fixtures_are_not_support_candidates(block):
    source = BlockVolume.empty((1, 6, 1))
    put(source, "minecraft:stone", (0, 0, 0))
    put(source, block, (2, 0, 0))
    context = spatial_context(source)
    assert context["columns"][0][2:4] == [2, 0]
    assert context["columns"][0][4][0] == [3, 5, 2, None, False]
    edited = source.clone()
    put(edited, "minecraft:stone", (3, 0, 0))
    assert fit_diagnostics(source, edited)["added_support_without_support_immediately_below"] == 1


def test_diagnostics_separate_real_changes_grounded_and_floating_additions():
    source = BlockVolume.empty((7, 8, 7))
    put(source, "minecraft:stone", (0, slice(None), slice(None)))
    edited = source.clone()
    put(edited, "minecraft:oak_planks", (slice(1, 4), 2, 2))
    put(edited, "minecraft:oak_planks", (5, 4, 4))
    put(edited, "minecraft:dirt", (0, 1, 1))
    put(edited, "minecraft:air", (0, 0, 0))
    report = fit_diagnostics(source, edited)
    assert report["changed_voxels"] == 6
    assert report["air_to_non_air_voxels"] == 4
    assert report["non_air_to_air_voxels"] == 1
    assert report["non_air_replaced_voxels"] == 1
    assert report["changed_bounds"] == [[0, 0, 0], [4, 5, 4]]
    assert report["added_support_without_support_immediately_below"] == 1
    assert report["of_those_without_lateral_support_either"] == 1
    assert report["unsupported_bottom_samples"] == [[4, 5, 4]]
    assert report["changed_on_boundary_voxels"] == 2
    assert report["source_highest_support_under_build"]["range_y"] == 0
    assert report["original_non_air_preserved_percent"] == round(100 * 47 / 49, 3)
    assert report["two_air_high_floor_candidates"]["lost"] == 2


def test_overhang_warning_is_local_not_a_connected_component_or_quality_rejection():
    source = BlockVolume.empty((5, 7, 3))
    put(source, "minecraft:stone", (0, slice(None), slice(None)))
    edited = source.clone()
    put(edited, "minecraft:stone", (slice(1, 5), 1, 0))
    put(edited, "minecraft:stone", (4, 1, slice(1, 5)))
    report = fit_diagnostics(source, edited)
    assert report["added_support_without_support_immediately_below"] == 4
    assert report["of_those_without_lateral_support_either"] == 0
    assert "not a failure" in " ".join(report["caveats"])
    assert report["source_highest_support_under_build"] == {
        "min_y": 0, "max_y": 0, "range_y": 0, "columns_without_source_support": 0,
    }


def test_semantic_palette_and_property_reordering_is_unchanged():
    source = BlockVolume.empty((2, 3, 2))
    put(source, "minecraft:oak_log[axis=y,waterlogged=false]", (0, 0, 0))
    edited = source.clone()
    edited.palette = ["minecraft:oak_log[waterlogged=false,axis=y]", "minecraft:air"]
    edited.blocks = 1 - source.blocks
    result = fit_diagnostics(source, edited)
    assert result["changed_voxels"] == 0
    assert result["changed_bounds"] is None
    assert result["unchanged_voxels_percent"] == 100


@pytest.mark.parametrize("dimensions", [(1, 1, 1), (32767, 1, 1), (1, 32767, 1), (1, 1, 32767), (199, 101, 199)])
def test_context_is_bounded_for_empty_and_extremely_skinny_volumes(dimensions):
    source = BlockVolume.empty(dimensions)
    source.blocks[::2, :, :] = source.palette_id("minecraft:stone")
    context = spatial_context(source)
    assert len(json.dumps(context).encode()) < 22000
    assert len(context["columns"]) <= 81
    assert len(context["tiles"]) <= 36
    assert len(context["vertical_profile"]) <= 32
    report = fit_diagnostics(source, source.clone())
    assert report["changed_voxels"] == 0
    assert len(json.dumps(report).encode()) < 12000


def test_bottom_of_selection_does_not_invent_support_below_it():
    source = BlockVolume.empty((2, 2, 2))
    edited = source.clone()
    put(edited, "minecraft:stone", (0, 0, 0))
    result = fit_diagnostics(source, edited)
    assert result["added_support_at_y0_below_selection_unknown"] == 1
    assert result["added_support_without_support_immediately_below"] == 0
    assert result["original_non_air_preserved_percent"] is None
    assert result["two_air_high_floor_candidates"]["gained_floor_xyz_samples"] == []
    assert spatial_context(source)["columns"][0] == [0, 0, -1, -1, [[0, 1, None, None, False]]]


def test_atlas_is_bounded_image_and_focused_original_coordinates(tmp_path, monkeypatch):
    source = BlockVolume.empty((17, 23, 19))
    put(source, "minecraft:stone", (slice(2, 5), slice(None), slice(None)))
    from worldprompt_core import spatial
    original_text = spatial.ImageDraw.ImageDraw.text
    drawn = []
    def record_text(self, xy, text, *args, **kwargs):
        drawn.append(text)
        return original_text(self, xy, text, *args, **kwargs)
    monkeypatch.setattr(spatial.ImageDraw.ImageDraw, "text", record_text)
    output = render_spatial_atlas(source, tmp_path / "focused.png", [[8, 7, 10], [16, 22, 18]])
    with Image.open(output) as image:
        assert image.size == (1536, 1024)
        image.verify()
    assert output.stat().st_size < 4 * 1024 * 1024
    assert "X = 10 | horizontal Z" in drawn
    assert "X = 12 | horizontal Z" in drawn
    assert "X = 14 | horizontal Z" in drawn
    assert "Z = 12 | horizontal X" in drawn
    assert "Z = 16 | horizontal X" in drawn
    assert "22" in drawn and "7" in drawn


@pytest.mark.parametrize("region", [[], [[0, 0], [1, 1]], [[-1, 0, 0], [1, 1, 1]],
                                    [[1, 0, 0], [0, 1, 1]], [[0, 0, 0], [3, 1, 1]],
                                    [[False, 0, 0], [1, 1, 1]]])
def test_atlas_rejects_invalid_regions(tmp_path, region):
    with pytest.raises(VolumeError):
        render_spatial_atlas(BlockVolume.empty((3, 3, 3)), tmp_path / "bad.png", region)


def test_diagnostics_reject_dimension_mismatch():
    with pytest.raises(VolumeError):
        fit_diagnostics(BlockVolume.empty((2, 3, 4)), BlockVolume.empty((2, 4, 3)))


@pytest.mark.parametrize("blocks", [np.zeros((0, 2, 2), dtype=np.uint32),
                                   np.zeros((2, 2), dtype=np.uint32),
                                   np.full((2, 2, 2), -1),
                                   np.zeros((2, 2, 2), dtype=float)])
def test_spatial_analysis_rejects_malformed_arrays(blocks):
    source = BlockVolume.empty((2, 2, 2))
    source.blocks = blocks
    with pytest.raises(VolumeError):
        spatial_context(source)


def test_air_state_change_is_counted_without_inventing_occupancy():
    source = BlockVolume.empty((2, 2, 2))
    edited = source.clone()
    put(edited, "minecraft:cave_air", (0, 0, 0))
    report = fit_diagnostics(source, edited)
    assert report["changed_voxels"] == report["air_state_changed_voxels"] == 1
    assert report["air_to_non_air_voxels"] == report["non_air_to_air_voxels"] == 0


def test_diagnostics_do_not_count_omitted_default_properties_as_changes():
    source = BlockVolume.empty((1, 1, 1))
    source.blocks[:] = source.palette_id("minecraft:oak_log[axis=y]")
    edited = BlockVolume(np.ones((1, 1, 1), dtype=np.uint32), ["minecraft:air", "minecraft:oak_log"])
    assert fit_diagnostics(source, edited)["changed_voxels"] == 0


@pytest.mark.parametrize("block,standable", [
    ("minecraft:iron_chain", False), ("minecraft:copper_chain", False),
    ("minecraft:exposed_copper_chain", False), ("minecraft:weathered_copper_chain", False),
    ("minecraft:oxidized_copper_chain", False), ("minecraft:waxed_copper_chain", False),
    ("minecraft:waxed_exposed_copper_chain", False), ("minecraft:waxed_weathered_copper_chain", False),
    ("minecraft:waxed_oxidized_copper_chain", False),
    ("minecraft:iron_chain[axis=y,waterlogged=false]", False),
    ("minecraft:chain_command_block", True), ("minecraft:stone", True)])
def test_chains_of_every_metal_are_not_standable_ground(block, standable):
    import json
    from pathlib import Path
    import worldprompt_core
    known = json.loads((Path(worldprompt_core.__file__).parent / "schemas" / "block-properties-26.3.json").read_text())
    assert block.split("[")[0] in known  # The ids are the real registry names, not guesses.
    source = BlockVolume.empty((1, 5, 1))
    put(source, "minecraft:stone", (0, 0, 0))
    put(source, block, (1, 0, 0))
    column = spatial_context(source)["columns"][0]
    assert column[2] == 1  # Occupied either way.
    assert column[3] == (1 if standable else 0)  # Standable ground is the chain itself or the stone below.


def test_every_registry_chain_is_excluded_and_chain_command_block_is_not():
    import json
    from pathlib import Path
    import worldprompt_core
    from worldprompt_core.spatial import palette_tables
    known = json.loads((Path(worldprompt_core.__file__).parent / "schemas" / "block-properties-26.3.json").read_text())
    chains = sorted(name for name in known if name.endswith("_chain"))
    assert "minecraft:iron_chain" in chains and "minecraft:copper_chain" in chains and len(chains) == 9
    _, support = palette_tables(chains + ["minecraft:chain_command_block"])
    assert not support[:-1].any() and support[-1]
