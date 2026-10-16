"""Bounded spatial evidence for planning and reviewing edits.

All public coordinates are volume-local xyz, with inclusive endpoints. Support
is a deliberately conservative block-name heuristic, not Minecraft collision or
physics simulation. Nothing in this module rejects an edit for aesthetic reasons.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw

from .block_state import VolumeError, parse_state, state_string
from .core import AIR, bounds_of, resolved_state
from .limits import MAX_BLOCKS
from .render import load_font, block_color


_NON_SUPPORT = {
    "water", "lava", "bubble_column", "fire", "soul_fire", "structure_void",
    "light", "short_grass", "tall_grass", "fern", "large_fern", "dead_bush",
    "bush", "short_dry_grass", "tall_dry_grass", "seagrass", "tall_seagrass",
    "kelp", "kelp_plant", "sugar_cane", "bamboo", "bamboo_sapling", "vine",
    "glow_lichen", "sculk_vein", "lily_pad", "frogspawn", "cobweb", "snow",
    "wheat", "carrots", "potatoes", "beetroots", "sweet_berry_bush", "cocoa",
    "nether_wart", "melon_stem", "pumpkin_stem", "attached_melon_stem",
    "attached_pumpkin_stem", "redstone_wire", "tripwire", "ladder", "lever",
    "rail", "powered_rail", "detector_rail", "activator_rail", "repeater",
    "comparator", "dandelion", "poppy", "blue_orchid", "allium", "azure_bluet",
    "oxeye_daisy", "cornflower", "lily_of_the_valley", "wither_rose", "sunflower",
    "lilac", "rose_bush", "peony", "pink_petals", "wildflowers", "leaf_litter",
    "spore_blossom", "torchflower", "pitcher_plant", "pitcher_crop", "torchflower_crop",
    "red_mushroom", "brown_mushroom", "crimson_fungus", "warped_fungus",
    "nether_sprouts", "hanging_roots", "pale_hanging_moss", "resin_clump",
    "pointed_dripstone", "iron_chain", "iron_bars", "end_rod", "flower_pot",
    "torch", "lantern", "candle", "amethyst_cluster", "tripwire_hook",
    "cactus", "cactus_flower", "open_eyeblossom", "closed_eyeblossom",
}
_NON_SUPPORT_ENDINGS = (
    "_leaves", "_sapling", "_tulip", "_roots", "_vines", "_vines_plant",
    "_torch", "_lantern", "_candle", "_carpet", "_button", "_pressure_plate",
    "_sign", "_hanging_sign", "_banner", "_coral", "_coral_fan",
    "_coral_wall_fan", "_pane", "_door", "_trapdoor", "_fence", "_fence_gate",
    "_wall", "_bed", "_slab", "_stairs", "_amethyst_bud", "_amethyst_cluster", "_chain",
)

# Material evidence only: these can also appear in player-built walls and roofs.
# Do not infer a biome or world-generation provenance from a block name.
_TERRAIN_NAMES = {
    "stone", "granite", "diorite", "andesite", "deepslate", "tuff", "calcite",
    "dripstone_block", "dirt", "coarse_dirt", "rooted_dirt", "grass_block",
    "podzol", "mycelium", "mud", "packed_mud", "clay", "gravel", "sand",
    "red_sand", "sandstone", "red_sandstone", "terracotta", "snow_block",
    "ice", "packed_ice", "blue_ice", "moss_block", "pale_moss_block",
    "netherrack", "soul_sand", "soul_soil", "basalt", "smooth_basalt",
    "blackstone", "end_stone", "crimson_nylium", "warped_nylium",
}


def _terrain_palette(volume):
    bases = [parse_state(state)[0] for state in volume.palette]
    terrain = np.asarray([
        base.startswith("minecraft:") and
        (base.split(":", 1)[1] in _TERRAIN_NAMES or base.endswith(("_ore", "_terracotta")))
        for base in bases], dtype=bool)
    return bases, terrain


def _tiles(width, length, count=4):
    xs = np.linspace(0, width, min(count, width) + 1, dtype=int)
    zs = np.linspace(0, length, min(count, length) + 1, dtype=int)
    for z0, z1 in zip(zs[:-1], zs[1:]):
        for x0, x1 in zip(xs[:-1], xs[1:]):
            yield int(x0), int(z0), int(x1), int(z1)


def _height_steps(heights):
    """Largest four-neighbor height difference, excluding absent columns."""
    steps = np.zeros_like(heights)
    for axis in (0, 1):
        before = (slice(None, -1), slice(None)) if axis == 0 else (slice(None), slice(None, -1))
        after = (slice(1, None), slice(None)) if axis == 0 else (slice(None), slice(1, None))
        lo, hi = heights[before], heights[after]
        delta = np.where((lo >= 0) & (hi >= 0), np.abs(hi - lo), 0)
        steps[before] = np.maximum(steps[before], delta)
        steps[after] = np.maximum(steps[after], delta)
    return steps


def terrain_context(volume):
    """Small landscape survey for both effort modes, without an extra model call.

    Exact regional aggregates complement at most 81 exact column samples. The
    planner still chooses the request's intent; no keyword classifier forces a
    building, biome, decorative density or minimum area of modification.
    """
    _validate(volume)
    bases, terrain_palette = _terrain_palette(volume)
    blocks = volume.blocks
    occupied = np.asarray([base not in AIR for base in bases], dtype=bool)[blocks]
    top = _heights(occupied)
    ground = _heights(terrain_palette[blocks])
    water = _heights(np.asarray([base in {"minecraft:water", "minecraft:bubble_column"} for base in bases])[blocks])
    lava = _heights(np.asarray([base == "minecraft:lava" for base in bases])[blocks])
    steps = _height_steps(ground)
    h, length, width = blocks.shape
    sample_points = [(x, z) for z in _sample_axes(length, 9) for x in _sample_axes(width, 9)]
    # Bound even palettes with many very long modded names; -1 means omitted.
    material_counts = {}
    for x, z in sample_points:
        for y in (top[z, x], ground[z, x]):
            if y >= 0:
                material = bases[int(blocks[y, z, x])]
                material_counts[material] = material_counts.get(material, 0) + 1
    materials = sorted(material_counts, key=lambda name: (-material_counts[name], name))[:16]
    material_ids = {name: index for index, name in enumerate(materials)}

    def material_at(x, y, z):
        return material_ids.get(bases[int(blocks[y, z, x])], -1) if y >= 0 else -1

    columns = []
    for x, z in sample_points:
        y, top_y = int(ground[z, x]), int(top[z, x])
        clearance = None
        if y >= 0:
            above = occupied[y + 1:, z, x]
            clearance = int(np.argmax(above)) if above.any() else h - y - 1
        columns.append([x, z, top_y, y, material_at(x, y, z), material_at(x, top_y, z),
                        int(water[z, x]), int(lava[z, x]), int(steps[z, x]), clearance])
    tiles = []
    for x0, z0, x1, z1 in _tiles(width, length):
        area = np.s_[z0:z1, x0:x1]
        heights = ground[area]
        valid = heights[heights >= 0]
        top_here = top[area]
        tiles.append([x0, z0, x1 - 1, z1 - 1, (x1 - x0) * (z1 - z0),
                      int(valid.size), int(valid.min()) if valid.size else None,
                      int(valid.max()) if valid.size else None,
                      round(float(np.median(valid)), 1) if valid.size else None,
                      int(((water[area] >= 0) & (water[area] == top_here)).sum()),
                      int(((lava[area] >= 0) & (lava[area] == top_here)).sum()),
                      int((steps[area] > 1).sum())])
    return {
        "coordinates": "local x east / y up / z south; inclusive; -1 height = absent in selection",
        "column_fields": ["x", "z", "highest_non_air_y", "highest_terrain_candidate_y",
                          "terrain_material_index", "top_material_index", "highest_water_y",
                          "highest_lava_y", "max_neighbor_terrain_height_step", "strict_air_cells_above_terrain"],
        "materials": materials, "columns": columns,
        "tile_fields": ["min_x", "min_z", "max_x", "max_z", "columns", "terrain_columns",
                        "min_terrain_y", "max_terrain_y", "median_terrain_y", "exposed_water_columns",
                        "exposed_lava_columns", "columns_with_neighbor_terrain_step_over_one"],
        "tiles": tiles,
        "caveats": [
            "At most 9x9 exact column samples; regional tiles scan all columns. Samples are not interpolation or complete feature coverage.",
            "Terrain candidates use natural full-block materials, not provenance: stone roofs can count; vegetation/logs do not. No biome data is supplied.",
            "Highest terrain does not prove solid ground beneath it; caves/overhangs can occur. Neighbor steps compare adjacent highest terrain, not true surface slope.",
            "Water/lava heights include covered fluids; exposed counts mean the fluid is the highest non-air block. Air clearance stops at the first non-air cell or selection top.",
            "Material index -1 means absent or omitted from the 16 most frequent sampled materials. Outside the selected volume is unknown.",
        ],
    }


def _validate(volume):
    blocks = volume.blocks
    if blocks.ndim != 3 or min(blocks.shape) < 1 or blocks.size > MAX_BLOCKS:
        raise VolumeError("Spatial analysis requires a nonempty bounded 3D volume")
    if (not np.issubdtype(blocks.dtype, np.integer) or not volume.palette
            or int(blocks.min()) < 0 or int(blocks.max()) >= len(volume.palette)):
        raise VolumeError("Spatial analysis encountered an invalid palette reference")


def _masks(volume):
    non_air, support = palette_tables(volume.palette)
    return non_air[volume.blocks], support[volume.blocks]


def palette_tables(palette):
    """Per-palette occupied and conservative standable-support flags."""
    non_air, support = [], []
    for state in palette:
        base, _ = parse_state(state)
        occupied = base not in AIR
        name = base.split(":", 1)[1]
        # Unknown/modded blocks are occupied, but are not assumed to support a floor.
        supporting = (occupied and base.startswith("minecraft:")
                      and name not in _NON_SUPPORT
                      and not name.startswith("potted_")
                      and not name.endswith(_NON_SUPPORT_ENDINGS))
        non_air.append(occupied)
        support.append(supporting)
    return np.asarray(non_air, dtype=bool), np.asarray(support, dtype=bool)


def _heights(mask):
    return np.where(mask.any(axis=0), mask.shape[0] - 1 - np.argmax(mask[::-1], axis=0), -1)


def _sample_axes(length, count):
    return np.unique(np.linspace(0, length - 1, min(count, length), dtype=int)).tolist()


def _air_runs(occupied, support, count=3):
    air = ~occupied
    starts = np.flatnonzero(air & np.r_[True, ~air[:-1]])
    ends = np.flatnonzero(air & np.r_[~air[1:], True])
    if not len(starts):
        return []
    lengths = ends - starts + 1
    # Stable ordering makes ties prefer the lower, usually more useful space.
    indices = np.argsort(-lengths, kind="stable")[:count]
    result = []
    for index in indices:
        lo, hi = int(starts[index]), int(ends[index])
        result.append([lo, hi, lo - 1 if lo else None,
                       hi + 1 if hi + 1 < len(air) else None,
                       bool(lo and support[lo - 1])])
    return result


def _standing_floors(occupied, support):
    """One candidate per support cell with two strictly-air cells immediately above."""
    return support[:-2] & ~occupied[1:-1] & ~occupied[2:] if len(occupied) >= 3 else support[:0]


def spatial_context(volume):
    """Return compact bounded evidence; tile counts scan their complete volume.

    Column samples and three longest air runs can miss important openings. Tiles
    contain whole-area aggregates, not a claim that every cell is buildable.
    """
    _validate(volume)
    occupied, support = _masks(volume)
    h, length, width = occupied.shape
    top, ground = _heights(occupied), _heights(support)
    columns = []
    for z in _sample_axes(length, 9):
        for x in _sample_axes(width, 9):
            columns.append([x, z, int(top[z, x]), int(ground[z, x]),
                            _air_runs(occupied[:, z, x], support[:, z, x])])
    standing = _standing_floors(occupied, support).sum(axis=0)
    x_edges = np.linspace(0, width, min(6, width) + 1, dtype=int)
    z_edges = np.linspace(0, length, min(6, length) + 1, dtype=int)
    tiles = []
    for z0, z1 in zip(z_edges[:-1], z_edges[1:]):
        for x0, x1 in zip(x_edges[:-1], x_edges[1:]):
            area_ground = ground[z0:z1, x0:x1]
            valid = area_ground[area_ground >= 0]
            area_top = top[z0:z1, x0:x1]
            tiles.append([int(x0), int(z0), int(x1 - 1), int(z1 - 1),
                          int((area_top >= 0).sum()), int(len(valid)),
                          int(valid.min()) if len(valid) else None,
                          int(valid.max()) if len(valid) else None,
                          int(standing[z0:z1, x0:x1].sum()),
                          int((standing[z0:z1, x0:x1] > 0).sum())])
    row_non_air = occupied.sum(axis=(1, 2))
    row_support = support.sum(axis=(1, 2))
    y_edges = np.linspace(0, h, min(32, h) + 1, dtype=int)
    profile = [[int(lo), int(hi - 1), int(row_non_air[lo:hi].sum()),
                int(row_support[lo:hi].sum())] for lo, hi in zip(y_edges[:-1], y_edges[1:])]
    return {
        "coordinates": "local xyz, inclusive; -1 height = no matching block; null floor/ceiling = outside selection",
        "dimensions": list(volume.dimensions),
        "column_fields": ["x", "z", "highest_non_air_y", "highest_support_candidate_y", "air_runs"],
        "air_run_fields": ["air_min_y", "air_max_y", "floor_y", "ceiling_y", "floor_is_support_candidate"],
        "columns": columns,
        "tile_fields": ["min_x", "min_z", "max_x", "max_z", "occupied_columns", "support_columns",
                        "min_highest_support_y", "max_highest_support_y", "two_air_high_floor_cells", "columns_with_two_air_high_floor"],
        "tiles": tiles,
        "vertical_fields": ["min_y", "max_y", "non_air_voxels", "support_candidate_voxels"],
        "vertical_profile": profile,
        "caveats": [
            "Columns sample at most 9x9 locations; only their three longest strictly-air runs are shown. Unlisted openings may exist.",
            "Tiles and vertical counts cover all cells. Highest support can be a roof or cave ceiling, not terrain; use air-run floors for caves.",
            "Support is a conservative name heuristic: excludes liquids, vegetation and thin/partial blocks (including stairs/slabs). No collision registry or physics simulation.",
            "Two-air-high floors are candidate standing cells only; no connectivity, doorway width, exterior access, or reachability is implied.",
        ],
    }


def _samples(mask, limit=12):
    # flatnonzero is at most one index per voxel, and never materializes xyz triples.
    flat = np.flatnonzero(mask)
    if len(flat) > limit:
        flat = flat[np.linspace(0, len(flat) - 1, limit, dtype=int)]
    return [[int(x), int(y), int(z)] for y, z, x in zip(*np.unravel_index(flat, mask.shape))]


def _landscape_diagnostics(source, edited, changed, added, removed):
    """Neutral coverage and source-surface evidence, not aesthetic constraints."""
    columns = changed.any(axis=0)
    _, length, width = changed.shape
    tiles = []
    for x0, z0, x1, z1 in _tiles(width, length):
        area = np.s_[:, z0:z1, x0:x1]
        tiles.append([x0, z0, x1 - 1, z1 - 1, (x1 - x0) * (z1 - z0),
                      int(columns[z0:z1, x0:x1].sum()), int(changed[area].sum()),
                      int(added[area].any(axis=0).sum()), int(removed[area].any(axis=0).sum())])
    distribution = {
        "changed_columns": int(columns.sum()),
        "changed_columns_percent": round(100 * int(columns.sum()) / columns.size, 3),
        "tiles_with_changes": sum(tile[5] > 0 for tile in tiles),
        "tile_fields": ["min_x", "min_z", "max_x", "max_z", "columns",
                        "changed_columns", "changed_voxels", "columns_with_additions", "columns_with_removals"],
        "tiles": tiles,
        "interpretation": "Exact final changes by region, not an operation sequence or a quality score. Compare to requested scope; sparse or unchanged tiles can be intentional.",
    }
    _, old_palette = _terrain_palette(source)
    _, new_palette = _terrain_palette(edited)
    old_ground, new_ground = _heights(old_palette[source.blocks]), _heights(new_palette[edited.blocks])
    valid = old_ground >= 0
    both = valid & (new_ground >= 0)
    surface_changed = np.take_along_axis(changed, np.maximum(old_ground, 0)[None], axis=0)[0] & valid
    surface_removed = np.take_along_axis(removed, np.maximum(old_ground, 0)[None], axis=0)[0] & valid
    terrain = {
        "source_surface_columns": int(valid.sum()),
        "source_surface_blocks_changed": int(surface_changed.sum()),
        "source_surface_blocks_became_air": int(surface_removed.sum()),
        "columns_with_lower_highest_terrain_candidate": int((both & (new_ground < old_ground)).sum()),
        "columns_with_higher_highest_terrain_candidate": int((both & (new_ground > old_ground)).sum()),
        "columns_losing_all_terrain_candidates": int((valid & (new_ground < 0)).sum()),
        "interpretation": "Natural-material heuristic, not terrain provenance. Buildings can affect heights. Assess requested landform preservation/reshaping using source and candidate views; these are not failure thresholds.",
    }
    return distribution, terrain


def fit_diagnostics(source, edited):
    """Exact voxel/count facts plus explicitly labelled local support heuristics."""
    _validate(source)
    _validate(edited)
    if source.dimensions != edited.dimensions:
        raise VolumeError("Fit diagnostics require matching volume dimensions")
    # Palette ordering and property serialization order do not constitute edits.
    ids = {}
    def mapping(palette):
        result = []
        for state in palette:
            normalized = state_string(*resolved_state(state))
            result.append(ids.setdefault(normalized, len(ids)))
        return np.asarray(result, dtype=np.uint32)
    old_ids, new_ids = mapping(source.palette), mapping(edited.palette)
    changed = old_ids[source.blocks] != new_ids[edited.blocks]
    old_occupied, old_support = _masks(source)
    new_occupied, new_support = _masks(edited)
    added = ~old_occupied & new_occupied
    removed = old_occupied & ~new_occupied
    added_support = added & new_support
    no_support_below = np.zeros_like(added_support)
    no_support_below[1:] = added_support[1:] & ~new_support[:-1]
    below_outside = int(added_support[0].sum())
    lateral = np.zeros_like(new_support)
    lateral[:, :, 1:] |= new_support[:, :, :-1]
    lateral[:, :, :-1] |= new_support[:, :, 1:]
    lateral[:, 1:, :] |= new_support[:, :-1, :]
    lateral[:, :-1, :] |= new_support[:, 1:, :]
    # A bridge or roof often lacks support immediately below but has lateral support.
    isolated_bottom = no_support_below & ~lateral
    faces = {"x_min": int(changed[:, :, 0].sum()), "x_max": int(changed[:, :, -1].sum()),
             "y_min": int(changed[0].sum()), "y_max": int(changed[-1].sum()),
             "z_min": int(changed[:, 0, :].sum()), "z_max": int(changed[:, -1, :].sum())}
    interior = changed[1:-1, 1:-1, 1:-1]
    boundary_count = int(changed.sum()) - int(interior.sum())
    old_floors = _standing_floors(old_occupied, old_support)
    new_floors = _standing_floors(new_occupied, new_support)
    new_floor_cells, lost_floor_cells = new_floors & ~old_floors, old_floors & ~new_floors
    build_columns = added_support.any(axis=0)
    ground = _heights(old_support)
    ground_under_build = ground[build_columns]
    valid_ground = ground_under_build[ground_under_build >= 0]
    changed_count = int(changed.sum())
    original_count = int(old_occupied.sum())
    preserved_occupied = int((old_occupied & ~changed).sum())
    distribution, terrain = _landscape_diagnostics(source, edited, changed, added, removed)
    return {
        "coordinates": "local xyz, inclusive; samples are bounded, counts are exact for their stated predicate",
        "dimensions": list(source.dimensions),
        "changed_voxels": changed_count,
        "air_to_non_air_voxels": int(added.sum()),
        "non_air_to_air_voxels": int(removed.sum()),
        "non_air_replaced_voxels": int((changed & old_occupied & new_occupied).sum()),
        "air_state_changed_voxels": int((changed & ~old_occupied & ~new_occupied).sum()),
        "changed_bounds": bounds_of(changed),
        "spatial_distribution": distribution,
        "terrain_surface": terrain,
        "unchanged_voxels_percent": round(100 * (1 - changed_count / changed.size), 3),
        "original_non_air_preserved_percent": round(100 * preserved_occupied / original_count, 3) if original_count else None,
        "changed_on_boundary_voxels": boundary_count,
        "changed_boundary_faces": faces,
        "added_support_candidate_voxels": int(added_support.sum()),
        "added_support_without_support_immediately_below": int(no_support_below.sum()),
        "of_those_without_lateral_support_either": int(isolated_bottom.sum()),
        "unsupported_bottom_samples": _samples(no_support_below),
        "isolated_bottom_samples": _samples(isolated_bottom),
        "added_support_at_y0_below_selection_unknown": below_outside,
        "columns_with_added_support": int(build_columns.sum()),
        "source_highest_support_under_build": {
            "min_y": int(valid_ground.min()) if len(valid_ground) else None,
            "max_y": int(valid_ground.max()) if len(valid_ground) else None,
            "range_y": int(np.ptp(valid_ground)) if len(valid_ground) else None,
            "columns_without_source_support": int((ground_under_build < 0).sum()),
        },
        "two_air_high_floor_candidates": {
            "source": int(old_floors.sum()), "edited": int(new_floors.sum()),
            "gained": int(new_floor_cells.sum()), "lost": int(lost_floor_cells.sum()),
            "gained_floor_xyz_samples": _samples(new_floor_cells),
            "lost_floor_xyz_samples": _samples(lost_floor_cells),
        },
        "caveats": [
            "Support tests use conservative block-name heuristics, excluding stairs/slabs and other partial blocks. They do not simulate Minecraft collisions or physics.",
            "Lack of immediate support is a review hint, not a failure: roofs, overhangs and bridges can intentionally span air. Attached components are not flood-filled.",
            "Source highest support can be a roof/cave ceiling. Height variation describes the footprint, not a collision or a requirement to flatten terrain.",
            "Headroom means strictly air above a support candidate. Counts do not establish room connectivity, door clearance, stair usability or access from outside.",
            "Boundary touches and source replacements can be intentional. Nothing here is an aesthetic quality score or a hard rejection.",
        ],
    }


def _region(volume, region):
    if region is None:
        return [0, 0, 0], [dimension - 1 for dimension in volume.dimensions]
    if (not isinstance(region, (list, tuple)) or len(region) != 2
            or any(not isinstance(point, (list, tuple)) or len(point) != 3 for point in region)):
        raise VolumeError("Spatial atlas region must be two inclusive xyz coordinates")
    low, high = region
    if any(type(a) is not int or type(b) is not int or not 0 <= a <= b < size
           for a, b, size in zip(low, high, volume.dimensions)):
        raise VolumeError("Spatial atlas region is outside the volume")
    return list(low), list(high)


def render_site_overview(isometric_path, top_path, path, *, label):
    """Keep both volume and whole-area evidence within one review image slot."""
    image = Image.new("RGB", (1536, 832), (15, 23, 31))
    draw = ImageDraw.Draw(image)
    draw.text((24, 16), f"{label} | whole selected volume", font=load_font(23), fill=(217, 226, 230))
    for index, (source, description) in enumerate((
        (isometric_path, "Exterior from above the south-east | X east / Y up / Z south"),
        (top_path, "Top | X increases right / Z increases down"),
    )):
        x0 = 16 + index * 768
        draw.rounded_rectangle((x0, 62, x0 + 736, 791), radius=5,
                               fill=(22, 32, 40), outline=(56, 72, 82))
        draw.text((x0 + 15, 78), description, font=load_font(17), fill=(212, 224, 230))
        with Image.open(source) as original:
            picture = original.convert("RGB")
            picture.thumbnail((704, 652))
            image.paste(picture, (x0 + 16 + (704 - picture.width) // 2,
                                  120 + (652 - picture.height) // 2))
    draw.text((24, 805), "Top view shows the highest non-air block, including leaves and fluids. Use section and numeric evidence for hidden geometry.",
              font=load_font(12), fill=(150, 171, 181))
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    image.quantize(colors=256, method=Image.Quantize.MEDIANCUT).save(path, "PNG", optimize=True)
    return path


def render_spatial_atlas(volume, path, region=None):
    """Six coordinate-labelled quarter/mid/three-quarter orthogonal sections.

    Optional region crops in original coordinates. Pixels use nearest-neighbour
    sampling; slices can miss thin features and are not complete geometry proofs.
    """
    _validate(volume)
    low, high = _region(volume, region)
    colors = np.asarray([block_color(state) for state in volume.palette], dtype=np.uint8)
    image = Image.new("RGB", (1536, 1024), (15, 23, 31))
    draw = ImageDraw.Draw(image)
    draw.text((24, 15), "Spatial sections | X east, Y up, Z south | local block coordinates", font=load_font(21), fill=(217, 226, 230))
    draw.text((24, 45), f"Inclusive region {low} to {high}. Exact slice positions; large slices are sampled to fit.", font=load_font(15), fill=(155, 174, 183))
    for row, axis in enumerate(("x", "z")):
        fixed_axis, horizontal_axis = (0, 2) if axis == "x" else (2, 0)
        for column, fraction in enumerate((0.25, 0.5, 0.75)):
            fixed = low[fixed_axis] + int(round((high[fixed_axis] - low[fixed_axis]) * fraction))
            x0, y0 = 16 + column * 508, 78 + row * 452
            x1, y1 = x0 + 492, y0 + 438
            draw.rounded_rectangle((x0, y0, x1, y1), radius=5, fill=(22, 32, 40), outline=(56, 72, 82))
            horizontal_name = "Z" if axis == "x" else "X"
            draw.text((x0 + 14, y0 + 10), f"{axis.upper()} = {fixed} | horizontal {horizontal_name}", font=load_font(17), fill=(212, 224, 230))
            if axis == "x":
                grid = volume.blocks[low[1]:high[1] + 1, low[2]:high[2] + 1, fixed]
            else:
                grid = volume.blocks[low[1]:high[1] + 1, fixed, low[0]:high[0] + 1]
            # Reduce palette IDs before RGB expansion, bounded even on very tall/narrow volumes.
            plot_w, plot_h = 414, 339
            scale = min(plot_w / grid.shape[1], plot_h / grid.shape[0])
            target_w, target_h = max(1, int(grid.shape[1] * scale)), max(1, int(grid.shape[0] * scale))
            horizontal_indices = np.minimum((np.arange(target_w) * grid.shape[1] / target_w).astype(int), grid.shape[1] - 1)
            vertical_indices = np.minimum((np.arange(target_h) * grid.shape[0] / target_h).astype(int), grid.shape[0] - 1)
            rgb = colors[grid[::-1][np.ix_(vertical_indices, horizontal_indices)]]
            tile = Image.fromarray(rgb)
            px = x0 + 55 + (plot_w - target_w) // 2
            py = y0 + 47 + (plot_h - target_h) // 2
            image.paste(tile, (px, py))
            draw.rectangle((px, py, px + target_w, py + target_h), outline=(92, 111, 121))
            horizontal_low, horizontal_high = low[horizontal_axis], high[horizontal_axis]
            for tick in (0, 0.5, 1):
                xx = px + round(tick * target_w)
                yy = py + round(tick * target_h)
                x_label = str(round(horizontal_low + tick * (horizontal_high - horizontal_low)))
                y_label = str(round(high[1] - tick * (high[1] - low[1])))
                draw.line((xx, py + target_h, xx, py + target_h + 4), fill=(166, 187, 197))
                draw.text((xx, py + target_h + 7), x_label, anchor="mt", font=load_font(12), fill=(166, 187, 197))
                draw.line((px - 4, yy, px, yy), fill=(166, 187, 197))
                draw.text((px - 7, yy), y_label, anchor="rm", font=load_font(12), fill=(166, 187, 197))
            draw.text((x0 + 14, y1 - 24), "Y up | cube colors; doors, stairs and fluids have simplified shapes", font=load_font(11), fill=(137, 156, 168))
    draw.text((24, 999), "Six slices are spatial evidence, not a full collision or accessibility check. Use the numeric column/air-run data for exact sampled heights.", font=load_font(12), fill=(150, 171, 181))
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    # Palette quantization bounds the PNG comfortably below 4 MiB even for a noisy material palette.
    image.quantize(colors=256, method=Image.Quantize.MEDIANCUT).save(path, "PNG", optimize=True)
    return path
