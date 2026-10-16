import pytest

from worldprompt_core.core import _replacement


@pytest.mark.parametrize("old,target,expected", [
    ("minecraft:spruce_log[axis=x]", "minecraft:crimson_stem", "minecraft:crimson_stem[axis=x]"),
    ("minecraft:oak_wood[axis=z]", "minecraft:warped_hyphae", "minecraft:warped_hyphae[axis=z]"),
    ("minecraft:oak_stairs[facing=west,half=top,shape=straight,waterlogged=true]",
     "minecraft:oak_trapdoor", "minecraft:oak_trapdoor[facing=west,half=top,waterlogged=true]"),
    # Same property name is insufficient: furnace facing cannot point up.
    ("minecraft:dispenser[facing=up,triggered=true]", "minecraft:furnace", "minecraft:furnace"),
    ("minecraft:dispenser[facing=east,triggered=true]", "minecraft:furnace", "minecraft:furnace[facing=east]"),
    ("minecraft:oak_log[axis=z]", "minecraft:stone", "minecraft:stone"),
    ("minecraft:oak_log[axis=z]", "minecraft:crimson_stem[axis=y]", "minecraft:crimson_stem[axis=y]"),
    ("minecraft:oak_log[axis=z]", "example:unknown_log", "example:unknown_log"),
    # Omitted properties are real game defaults, and must survive replacement.
    ("minecraft:oak_log", "minecraft:bone_block", "minecraft:bone_block[axis=y]"),
    ("minecraft:oak_stairs", "minecraft:furnace", "minecraft:furnace[facing=north]"),
])
def test_only_target_supported_property_values_survive(old, target, expected):
    assert _replacement(old, target, True) == expected
    assert _replacement(old, target, False) == target


def test_compatibility_is_shared_with_authentic_replay(tmp_path):
    from worldprompt_core.core import BlockVolume, apply_plan
    from worldprompt_core.replay import compile_replay
    source = BlockVolume.empty((1, 1, 1))
    source.blocks[0, 0, 0] = source.palette_id("minecraft:spruce_log[axis=x]")
    plan = {"version": 1, "summary": "Change wood", "operations": [{
        "op": "replace", "min": [0, 0, 0], "max": [0, 0, 0],
        "match": ["minecraft:spruce_log"], "block": "minecraft:crimson_stem",
        "preserve_properties": True,
    }]}
    edited, _ = apply_plan(source, plan)
    manifest = compile_replay(source, plan, tmp_path / "replay").manifest
    assert "minecraft:crimson_stem[axis=x]" in manifest["palette"]
    assert edited.palette[int(edited.blocks[0, 0, 0])] == "minecraft:crimson_stem[axis=x]"
