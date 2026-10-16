from PIL import Image
from worldprompt_core.core import BlockVolume, apply_plan
import numpy as np
import pytest
from worldprompt_core.render import block_color, camera_basis, normalize_camera, render_camera, render_preview, render_slice


def test_stairs_are_not_colored_as_air():
    assert block_color("minecraft:oak_stairs") == block_color("minecraft:oak_planks")
    assert block_color("minecraft:cave_air") != block_color("minecraft:oak_stairs")


def test_previews_are_readable_pngs(tmp_path):
    source = BlockVolume.empty((16, 12, 16))
    plan = {"version": 1, "summary": "House", "operations": [
        {"op": "fill", "min": [0, 0, 0], "max": [15, 0, 15], "block": "minecraft:grass_block"},
        {"op": "hollow_box", "min": [3, 1, 3], "max": [12, 7, 12], "block": "minecraft:oak_planks", "thickness": 1},
        {"op": "fill", "min": [6, 1, 12], "max": [7, 3, 12], "block": "minecraft:air"},
        {"op": "fill", "min": [2, 8, 2], "max": [13, 8, 13], "block": "minecraft:deepslate_bricks"}]}
    edited, _ = apply_plan(source, plan)
    preview = render_preview(edited, tmp_path / "preview.png")
    section = render_slice(edited, tmp_path / "slice.png", "y", 3)
    with Image.open(preview) as image:
        assert image.size == (1200, 850)
        assert len(image.getcolors(1_000_000)) > 10
    with Image.open(section) as image:
        assert image.format == "PNG"


def test_camera_angles_follow_minecraft_conventions():
    for yaw, pitch, forward in ((0, 0, (0, 0, 1)), (90, 0, (-1, 0, 0)), (180, 0, (0, 0, -1)),
                                (-90, 0, (1, 0, 0)), (0, 90, (0, -1, 0))):
        f, right, up = camera_basis(yaw, pitch)
        np.testing.assert_allclose(f, forward, atol=1e-9)
        assert abs(f @ right) < 1e-9 and abs(f @ up) < 1e-9
    # Looking south, east (+x) is on the left of the screen.
    assert camera_basis(0, 0)[1][0] == -1


@pytest.mark.parametrize("camera", [
    {"position": [0, 0, 0], "yaw": 0}, {"position": [0, 0], "yaw": 0, "pitch": 0},
    {"position": [0, 0, 0], "yaw": 0, "pitch": 91}, {"position": [0, 0, 1e9], "yaw": 0, "pitch": 0},
    {"position": [0, 0, 0], "yaw": 0, "pitch": 0, "fov": 5}, {"position": [0, 0, 0], "yaw": True, "pitch": 0},
    {"position": [0, 0, 0], "yaw": 0, "pitch": 0, "roll": 0}, {"position": [0, float("nan"), 0], "yaw": 0, "pitch": 0},
])
def test_invalid_cameras_are_rejected(camera):
    with pytest.raises(ValueError):
        normalize_camera(camera, (8, 8, 8))


def test_camera_views_show_the_side_they_face(tmp_path):
    source = BlockVolume.empty((8, 8, 8))
    source.blocks[:, :, 0] = source.palette_id("minecraft:red_wool")    # west face, x = 0
    source.blocks[:, :, 7] = source.palette_id("minecraft:blue_wool")   # east face, x = 7
    red, blue = block_color("minecraft:red_wool"), block_color("minecraft:blue_wool")

    def colors(camera):
        report = {}
        with Image.open(render_camera(source, tmp_path / "view.png", camera, report=report)) as image:
            assert image.size == (768, 544)
            found = {color for _, color in image.getcolors(1_000_000)}
        return found, report
    # Standing west looking east sees only the red wall's outer face.
    west, report = colors({"position": [-12, 4, 4], "yaw": -90, "pitch": 0})
    assert report["faces_drawn"] > 0 and report["forward"] == [1, 0, 0] and report["camera"]["fov"] == 70
    assert any(c == tuple(int(v * 0.7) for v in red) for c in west)
    assert not any(c == tuple(int(v * 0.77) for v in blue) for c in west)
    east, _ = colors({"position": [20, 4, 4], "yaw": 90, "pitch": 0, "fov": 50})
    assert any(c == tuple(int(v * 0.77) for v in blue) for c in east)
    # Facing away from the volume draws no blocks but still renders.
    _, away = colors({"position": [-12, 4, 4], "yaw": 90, "pitch": 0})
    assert away["faces_drawn"] == 0


def test_isometric_preview_reports_stride(tmp_path):
    report = {}
    render_preview(BlockVolume.empty((300, 4, 4)), tmp_path / "wide.png", report=report)
    assert report["stride"] == 3
