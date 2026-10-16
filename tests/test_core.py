
import numpy as np
import pytest

from worldprompt_core.core import BlockVolume, VolumeError, apply_plan


def plan(*operations):
    return {"version": 1, "summary": "Test edit", "operations": list(operations)}


def fill(minimum=(0, 0, 0), maximum=(2, 2, 2), block="minecraft:stone"):
    return {"op": "fill", "min": list(minimum), "max": list(maximum), "block": block}


@pytest.mark.parametrize("dimensions", [[], [1, 2], [True, 1, 1], [1.0, 1, 1], [0, 1, 1], [32768, 1, 1], [32767, 32767, 32767]])
def test_volume_dimensions_are_bounded(dimensions):
    with pytest.raises(VolumeError):
        BlockVolume.empty(dimensions)


def test_clone_and_summary_preserve_local_geometry():
    source = BlockVolume.empty((5, 6, 7), data_version=5000)
    source.blocks[2:4, 1:5, 2:4] = source.palette_id("minecraft:oak_stairs[facing=east,half=bottom]")
    edited = source.clone()
    edited.blocks[0] = edited.palette_id("minecraft:stone")
    assert not source.blocks[0].any()
    assert source.palette == ["minecraft:air", "minecraft:oak_stairs[facing=east,half=bottom]"]
    assert edited.data_version == source.data_version == 5000
    assert source.summary()["dimensions"] == [5, 6, 7]
    assert source.summary()["non_air_blocks"] == 16
    assert source.summary()["non_air_bounds"] == [[2, 2, 1], [3, 3, 4]]


def test_fill_bounds_are_inclusive_and_transactional():
    source = BlockVolume.empty((7, 6, 5))
    edited, report = apply_plan(source, plan(fill((1, 2, 3), (3, 4, 4))))
    assert report["changed_blocks"] == 18
    assert report["changed_bounds"] == [[1, 2, 3], [3, 4, 4]]
    assert not source.blocks.any()
    assert edited.dimensions == source.dimensions
    with pytest.raises(VolumeError, match="outside"):
        apply_plan(source, plan(fill(), fill(maximum=(7, 0, 0))))
    assert not source.blocks.any()
    for bad in [fill(minimum=(1.0, 0, 0)), fill(minimum=(True, 0, 0)), fill(minimum=(-1, 0, 0))]:
        with pytest.raises(VolumeError):
            apply_plan(source, plan(bad))


def test_single_uses_one_coordinate_and_block_list_preview_uses_first_state():
    source = BlockVolume.empty((3, 2, 2))
    operation = {"op": "single", "at": [2, 1, 1],
                 "block": ["minecraft:stone", "minecraft:dirt"]}
    edited, report = apply_plan(source, plan(operation))
    assert report["changed_blocks"] == 1
    assert edited.palette[int(edited.blocks[1, 1, 2])] == "minecraft:stone"
    for bad in ([], ["minecraft:stone", "minecraft:invented"], ["minecraft:stone"] * 257):
        with pytest.raises(VolumeError):
            apply_plan(source, plan({**operation, "block": bad}))
    with pytest.raises(VolumeError):
        apply_plan(source, plan({**operation, "at": [3, 1, 1]}))


@pytest.mark.parametrize("operation", [
    {"op": "fill", "min": [0, 0, 0], "max": [2, 0, 2]},
    {"op": "single", "at": [1, 1, 1]},
    {"op": "replace", "min": [0, 0, 0], "max": [2, 2, 2],
     "match": ["minecraft:air"], "preserve_properties": False},
    {"op": "hollow_box", "min": [0, 0, 0], "max": [2, 2, 2], "thickness": 1},
    {"op": "solid_ellipsoid", "min": [0, 0, 0], "max": [2, 2, 2]},
    {"op": "hollow_ellipsoid", "min": [0, 0, 0], "max": [2, 2, 2], "thickness": 1},
    {"op": "solid_cylinder", "min": [0, 0, 0], "max": [2, 2, 2], "axis": "y"},
    {"op": "hollow_cylinder", "min": [0, 0, 0], "max": [2, 2, 2], "axis": "x", "thickness": 1},
    {"op": "line", "start": [0, 0, 0], "end": [2, 2, 2]},
])
def test_every_operation_accepts_a_block_list(operation):
    source = BlockVolume.empty((3, 3, 3))
    edited, report = apply_plan(source, plan({**operation,
        "block": ["minecraft:stone", "minecraft:dirt"]}))
    assert report["changed_blocks"] > 0
    assert "minecraft:stone" in edited.palette


def test_replace_preserves_orientation_and_does_not_chain():
    source = BlockVolume.empty((3, 1, 1))
    old = "minecraft:oak_stairs[facing=west,half=top,shape=straight,waterlogged=false]"
    source.blocks[0, 0, 0] = source.palette_id(old)
    source.blocks[0, 0, 1] = source.palette_id("minecraft:spruce_stairs[facing=east,half=bottom,shape=straight,waterlogged=false]")
    operation = {"op": "replace", "min": [0, 0, 0], "max": [2, 0, 0], "match": ["minecraft:oak_stairs"],
                 "block": "minecraft:spruce_stairs", "preserve_properties": True}
    edited, report = apply_plan(source, plan(operation))
    assert "facing=west" in edited.palette[int(edited.blocks[0, 0, 0])]
    assert report["changed_blocks"] == 1
    operation["block"] = "minecraft:stone"
    edited, _ = apply_plan(source, plan(operation))
    assert edited.palette[int(edited.blocks[0, 0, 0])] == "minecraft:stone"


def test_hollow_shell_preserves_interior_and_line():
    source = BlockVolume.empty((5, 5, 5))
    source.blocks[2, 2, 2] = source.palette_id("minecraft:gold_block")
    shell = {"op": "hollow_box", "min": [0, 0, 0], "max": [4, 4, 4], "block": "minecraft:stone", "thickness": 1}
    edited, report = apply_plan(source, plan(shell))
    assert report["changed_blocks"] == 125 - 27
    assert edited.palette[int(edited.blocks[2, 2, 2])] == "minecraft:gold_block"
    line = {"op": "line", "start": [0, 0, 0], "end": [4, 4, 4], "block": "minecraft:oak_log[axis=y]"}
    _, line_report = apply_plan(source, plan(line))
    assert line_report["changed_blocks"] == 5


def test_ellipsoid_thickness_stays_in_bounds_and_preserves_interior():
    source = BlockVolume.empty((9, 9, 9))
    operation = {"op": "hollow_ellipsoid", "min": [0, 0, 0], "max": [8, 8, 8], "block": "minecraft:stone", "thickness": 1}
    edited, report = apply_plan(source, plan(operation))
    assert 0 < report["changed_blocks"] < 729
    assert edited.blocks[4, 4, 4] == 0
    assert edited.blocks[4, 4, 7] == 0
    assert edited.blocks[0, 0, 0] == 0
    operation["thickness"] = 2
    thicker, thicker_report = apply_plan(source, plan(operation))
    assert thicker.blocks[4, 4, 4] == 0
    assert thicker.blocks[4, 4, 7] != 0
    assert thicker_report["changed_blocks"] > report["changed_blocks"]
    solid, _ = apply_plan(source, plan({"op": "solid_ellipsoid", "min": [0, 0, 0],
                                        "max": [8, 8, 8], "block": "minecraft:stone"}))
    assert solid.blocks[4, 4, 4] != 0
    assert not source.blocks.any()


def test_hollow_ellipsoid_rejects_thickness_without_a_cavity():
    source = BlockVolume.empty((5, 5, 5))
    operation = {"op": "hollow_ellipsoid", "min": [0, 0, 0], "max": [4, 4, 4],
                 "block": "minecraft:stone", "thickness": 1}
    for thickness in (0, True, 3):
        with pytest.raises(VolumeError, match="thickness"):
            apply_plan(source, plan({**operation, "thickness": thickness}))
    with pytest.raises(VolumeError, match="unsupported"):
        apply_plan(source, plan({**operation, "op": "ellipsoid", "hollow": True}))


@pytest.mark.parametrize("axis", ["x", "y", "z"])
def test_cylinders_extrude_an_inscribed_section_along_their_axis(axis):
    source = BlockVolume.empty((9, 9, 9))
    source.blocks[4, 4, 4] = source.palette_id("minecraft:gold_block")
    operation = {"op": "solid_cylinder", "min": [0, 0, 0], "max": [8, 8, 8], "block": "minecraft:stone", "axis": axis}
    solid, report = apply_plan(source, plan(operation))
    occupied = solid.blocks != 0
    along = {"x": 2, "y": 0, "z": 1}[axis]
    # Every slice across the axis is the same disc, and box corners stay empty.
    sections = np.moveaxis(occupied, along, 0)
    assert all((section == sections[0]).all() for section in sections)
    assert sections[0].sum() < 81 and sections[0][4, 4] and not sections[0][0, 0]
    assert report["changed_blocks"] == occupied.sum()
    tube, _ = apply_plan(source, plan({**operation, "op": "hollow_cylinder", "thickness": 2}))
    tube_sections = np.moveaxis(tube.blocks != 0, along, 0)
    # The interior and the open ends keep the source; the wall is 2 thick.
    assert tube.palette[int(tube.blocks[4, 4, 4])] == "minecraft:gold_block"
    assert tube_sections[0][4, 1] and not tube_sections[0][4, 2]
    assert not source.blocks[0].any()


def test_cylinder_axis_and_thickness_are_checked():
    source = BlockVolume.empty((5, 2, 5))
    operation = {"op": "hollow_cylinder", "min": [0, 0, 0], "max": [4, 1, 4],
                 "block": "minecraft:stone", "axis": "y", "thickness": 3}
    with pytest.raises(VolumeError, match="thickness"):
        apply_plan(source, plan(operation))
    # Only the 5x5 cross-section limits the wall, not the 2-high axis.
    apply_plan(source, plan({**operation, "thickness": 2}))
    for bad in ({"axis": "w"}, {"axis": None}, {"thickness": 0}, {"thickness": True}):
        with pytest.raises(VolumeError):
            apply_plan(source, plan({**operation, "thickness": 1, **bad}))
    with pytest.raises(VolumeError, match="expected fields"):
        apply_plan(source, plan({**operation, "op": "solid_cylinder"}))


def test_bogus_operation_and_state_do_not_mutate():
    source = BlockVolume.empty((4, 4, 4))
    with pytest.raises(VolumeError, match="unsupported"):
        apply_plan(source, plan({"op": "execute", "code": "oops"}))
    with pytest.raises(VolumeError, match="Invalid block"):
        apply_plan(source, plan(fill(block="../../path")))
    assert source.palette == ["minecraft:air"]


def test_plan_type_edges_and_explicit_noop():
    source = BlockVolume.empty((4, 4, 4))
    bad = plan(fill())
    bad["version"] = True
    with pytest.raises(VolumeError):
        apply_plan(source, bad)
    with pytest.raises(VolumeError, match="unsupported"):
        apply_plan(source, plan({"op": []}))
    with pytest.raises(VolumeError, match="No changes proposed: Cannot fit"):
        apply_plan(source, {"version": 1, "summary": "Cannot fit", "operations": []})


def test_property_order_does_not_create_a_false_block_change():
    source = BlockVolume.empty((1, 1, 1))
    original = "minecraft:oak_stairs[waterlogged=false,facing=east]"
    source.blocks[:] = source.palette_id(original)
    operation = {"op": "replace", "min": [0, 0, 0], "max": [0, 0, 0],
                 "match": ["minecraft:oak_stairs[facing=east,waterlogged=false]"],
                 "block": "minecraft:oak_stairs", "preserve_properties": True}
    edited, report = apply_plan(source, plan(operation))
    assert report["changed_blocks"] == 0
    assert edited.palette == source.palette


def test_omitted_defaults_reuse_captured_state_without_a_false_change():
    source = BlockVolume.empty((1, 1, 1))
    source.blocks[:] = source.palette_id("minecraft:oak_log[axis=y]")
    edited, report = apply_plan(source, plan(fill((0, 0, 0), (0, 0, 0), "minecraft:oak_log")))
    assert report["changed_blocks"] == 0
    assert edited.palette == source.palette


def test_later_exact_replacement_resolves_defaults_of_prior_placement():
    source = BlockVolume.empty((2, 1, 1))
    operation = {"op": "replace", "min": [0, 0, 0], "max": [1, 0, 0],
                 "match": ["minecraft:oak_log[axis=y]"], "block": "minecraft:gold_block",
                 "preserve_properties": False}
    edited, report = apply_plan(source, plan(
        fill((0, 0, 0), (0, 0, 0), "minecraft:oak_log"),
        fill((1, 0, 0), (1, 0, 0), "minecraft:oak_log[axis=x]"), operation))
    assert report["operation_counts"][-1]["changed_blocks"] == 1
    assert edited.palette[int(edited.blocks[0, 0, 0])] == "minecraft:gold_block"
    assert edited.palette[int(edited.blocks[0, 0, 1])] == "minecraft:oak_log[axis=x]"


def test_bracketed_match_uses_defaults_for_omitted_properties():
    source = BlockVolume.empty((2, 1, 1))
    source.blocks[0, 0, 0] = source.palette_id("minecraft:oak_stairs[facing=east,half=bottom,shape=straight,waterlogged=false]")
    source.blocks[0, 0, 1] = source.palette_id("minecraft:oak_stairs[facing=east,half=top,shape=straight,waterlogged=false]")
    edited, report = apply_plan(source, plan({
        "op": "replace", "min": [0, 0, 0], "max": [1, 0, 0],
        "match": ["minecraft:oak_stairs[facing=east]"], "block": "minecraft:gold_block",
        "preserve_properties": False}))
    assert report["changed_blocks"] == 1
    assert edited.palette[int(edited.blocks[0, 0, 0])] == "minecraft:gold_block"
    assert edited.blocks[0, 0, 1] == source.blocks[0, 0, 1]


def test_bounds_of_matches_argwhere_reference_without_allocating_positions():
    from worldprompt_core.core import bounds_of
    rng = np.random.default_rng(7)
    for shape in ((1, 1, 1), (4, 5, 6), (9, 1, 3), (2, 8, 1)):
        for density in (0.0, 0.02, 0.5, 1.0):
            mask = rng.random(shape) < density
            positions = np.argwhere(mask)
            expected = None if not len(positions) else [
                [int(positions[:, 2].min()), int(positions[:, 0].min()), int(positions[:, 1].min())],
                [int(positions[:, 2].max()), int(positions[:, 0].max()), int(positions[:, 1].max())]]
            assert bounds_of(mask) == expected
    one = np.zeros((4, 5, 6), dtype=bool)
    one[3, 1, 5] = True  # (y, z, x) -> public [x, y, z]
    assert bounds_of(one) == [[5, 3, 1], [5, 3, 1]]


def test_resolved_state_is_cached_but_callers_cannot_corrupt_it():
    from worldprompt_core.core import resolved_state, _resolved_state
    first = resolved_state("minecraft:oak_stairs[half=top]")
    assert first == ("minecraft:oak_stairs", {"facing": "north", "half": "top", "shape": "straight", "waterlogged": "false"})
    first[1]["facing"] = "south"
    first[1]["extra"] = "x"
    assert resolved_state("minecraft:oak_stairs[half=top]")[1]["facing"] == "north"
    assert "extra" not in resolved_state("minecraft:oak_stairs[half=top]")[1]
    hits = _resolved_state.cache_info().hits
    resolved_state("minecraft:oak_stairs[half=top]")
    assert _resolved_state.cache_info().hits == hits + 1
    assert resolved_state("minecraft:stone") == ("minecraft:stone", {})
    assert resolved_state("custom:thing[a=b]") == ("custom:thing", {"a": "b"})
    for bad in ("not a state", "minecraft:stone[", ["minecraft:stone"], {"a": 1}, None, 5, b"minecraft:stone"):
        with pytest.raises(VolumeError):
            resolved_state(bad)
