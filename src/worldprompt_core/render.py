"""Deterministic, dependency-light previews for people and the editing worker."""
from __future__ import annotations

import colorsys
import hashlib
import math
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont

from .block_state import VolumeError, parse_state
from .core import AIR
from .limits import DEFAULT_CAMERA_FOV, MAX_CAMERA_DISTANCE, MAX_CAMERA_FOV, MIN_CAMERA_FOV

# Size of every preview image attached to a planner pass.
PREVIEW_WIDTH, PREVIEW_HEIGHT = 768, 544


def block_color(state):
    base = parse_state(state)[0]
    if base in AIR:
        return (15, 23, 31)
    name = base.split(":", 1)[1]
    families = [("glass", (113, 180, 195)), ("water", (54, 108, 189)),
                ("lava", (232, 102, 37)), ("snow", (222, 236, 242)), ("quartz", (216, 216, 201)),
                ("calcite", (212, 215, 199)), ("sandstone", (206, 190, 133)), ("sand", (207, 193, 135)),
                ("deepslate", (74, 83, 94)), ("blackstone", (58, 54, 68)), ("obsidian", (45, 38, 64)),
                ("copper", (158, 108, 72)), ("iron", (181, 184, 180)), ("gold", (226, 181, 53)),
                ("diamond", (65, 202, 196)), ("emerald", (44, 182, 118)), ("amethyst", (152, 114, 196)),
                ("leaves", (76, 127, 66)), ("spruce", (111, 82, 51)), ("dark_oak", (87, 64, 42)), ("birch", (196, 183, 133)),
                ("cherry", (208, 143, 147)), ("mangrove", (119, 65, 57)), ("acacia", (175, 95, 55)),
                ("oak", (164, 127, 77)), ("jungle", (164, 123, 81)), ("crimson", (113, 56, 76)),
                ("warped", (55, 125, 119)), ("bamboo", (182, 178, 80)),
                ("grass", (107, 144, 71)), ("moss", (103, 128, 68)), ("dirt", (130, 93, 66)),
                ("mud", (103, 94, 82)), ("stone", (139, 147, 151)), ("brick", (149, 96, 80)),
                ("torch", (246, 194, 79)), ("lantern", (237, 177, 72)), ("glow", (235, 199, 108))]
    dyes = {"white": (222, 225, 222), "orange": (210, 112, 47), "magenta": (167, 75, 162),
            "light_blue": (101, 163, 205), "yellow": (222, 187, 60), "lime": (139, 190, 65),
            "pink": (211, 137, 165), "gray": (87, 95, 97), "light_gray": (167, 172, 165),
            "cyan": (58, 131, 150), "purple": (112, 73, 164), "blue": (67, 85, 166),
            "brown": (114, 83, 58), "green": (82, 116, 60), "red": (174, 64, 61), "black": (41, 44, 50)}
    for dye in sorted(dyes, key=len, reverse=True):
        if name.startswith(dye + "_"):
            return dyes[dye]
    for fragment, color in families:
        if fragment in name:
            return color
    digest = hashlib.sha256(state.encode()).digest()
    return tuple(int(channel * 255) for channel in colorsys.hsv_to_rgb(digest[0] / 255, 0.28, 0.63))


def load_font(size):
    for location in ("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", "C:/Windows/Fonts/segoeui.ttf"):
        try:
            return ImageFont.truetype(location, size)
        except OSError:
            pass
    return ImageFont.load_default(size=size)


ISOMETRIC_VIEW = ("fixed isometric view from above the south-east, looking north-west and down; "
                  "origin (0,0,0) is the far corner of the ground outline; +X runs down-right, "
                  "+Z down-left; north, west and bottom faces are hidden")


def render_preview(volume, path, width=1200, height=850, report=None):
    """Render visible cube faces. State shapes/textures are intentionally simplified.

    The camera is fixed (see ``ISOMETRIC_VIEW``). ``report``, when a dict,
    receives the sampling stride actually used.
    """
    path = Path(path)
    original = volume.blocks
    solid_table = np.array([parse_state(s)[0] not in AIR for s in volume.palette])
    stride = max(1, math.ceil(max(original.shape) / 120))
    while True:
        blocks = original[::stride, ::stride, ::stride]
        solid = solid_table[blocks]
        # Only the three faces visible from +x,+y,+z need rendering.
        top = solid & ~np.pad(solid[1:], ((0, 1), (0, 0), (0, 0)))
        east = solid & ~np.pad(solid[:, :, 1:], ((0, 0), (0, 0), (0, 1)))
        south = solid & ~np.pad(solid[:, 1:, :], ((0, 0), (0, 1), (0, 0)))
        positions = np.argwhere(top | east | south)
        if len(positions) <= 45_000 or stride >= max(original.shape):
            break
        stride += 1
    h, l, w = blocks.shape
    scale = min((width - 160) / max(w + l, 1), (height - 185) / max((w + l) * 0.5 + h, 1))
    ox = (width - (w - l) * scale) / 2
    oy = 95 + h * scale
    image = Image.new("RGB", (width, height), (15, 23, 31))
    draw = ImageDraw.Draw(image)
    def project(x, y, z):
        return (ox + (x - z) * scale, oy + ((x + z) * 0.5 - y) * scale)
    plane = [project(0, 0, 0), project(w, 0, 0), project(w, 0, l), project(0, 0, l)]
    draw.polygon(plane, fill=(24, 37, 44), outline=(49, 68, 73))
    grid_step = max(1, math.ceil(max(w, l) / 24))
    for x in range(0, w + 1, grid_step):
        draw.line([project(x, 0, 0), project(x, 0, l)], fill=(37, 51, 57))
    for z in range(0, l + 1, grid_step):
        draw.line([project(0, 0, z), project(w, 0, z)], fill=(37, 51, 57))
    positions = sorted(positions, key=lambda p: (int(p[0] + p[1] + p[2]), int(p[0])))
    colors = [block_color(s) for s in volume.palette]
    for y, z, x in positions:
        x, y, z = int(x), int(y), int(z)
        color = colors[int(blocks[y, z, x])]
        # Side faces first; top last makes edge treatment consistent.
        faces = []
        if east[y, z, x]:
            faces.append(([(x + 1, y, z), (x + 1, y, z + 1), (x + 1, y + 1, z + 1), (x + 1, y + 1, z)], 0.77))
        if south[y, z, x]:
            faces.append(([(x, y, z + 1), (x + 1, y, z + 1), (x + 1, y + 1, z + 1), (x, y + 1, z + 1)], 0.58))
        if top[y, z, x]:
            faces.append(([(x, y + 1, z), (x + 1, y + 1, z), (x + 1, y + 1, z + 1), (x, y + 1, z + 1)], 1.08))
        for corners, shade in faces:
            fill = tuple(min(255, int(c * shade)) for c in color)
            draw.polygon([project(*corner) for corner in corners], fill=fill)
    # Axis labels on the ground outline make the fixed camera unambiguous.
    for corner, text, anchor in (((0, 0, 0), "0,0,0", "md"), ((w, 0, 0), "+X east", "lm"),
                                 ((0, 0, l), "+Z south", "rm")):
        px, py = project(*corner)
        offset = {"md": (0, -6), "lm": (8, 0), "rm": (-8, 0)}[anchor]
        draw.text((px + offset[0], py + offset[1]), text, anchor=anchor, font=load_font(14), fill=(178, 220, 205))
    draw.text((42, 27), "WorldPrompt preview · viewed from above the south-east", font=load_font(21), fill=(178, 220, 205))
    w0, h0, l0 = volume.dimensions
    draw.text((42, height - 66), f"{w0} × {h0} × {l0} blocks  ·  X east / Y up / Z south", font=load_font(18), fill=(171, 186, 192))
    detail = "Material colors · cube preview · Minecraft shapes and textures differ"
    if stride > 1:
        detail += f" · sampled every {stride} blocks"
    draw.text((42, height - 37), detail, font=load_font(14), fill=(113, 134, 145))
    if not len(positions):
        draw.text((width // 2 - 95, height // 2), "Empty selection", font=load_font(22), fill=(170, 191, 198))
    path.parent.mkdir(parents=True, exist_ok=True)
    image.save(path, "PNG")
    if report is not None:
        report["stride"] = stride
    return path


# Face directions with their outward normals, unit-cube corners and shading.
_FACES = (
    ((1, 0, 0), ((1, 0, 0), (1, 0, 1), (1, 1, 1), (1, 1, 0)), 0.77),
    ((-1, 0, 0), ((0, 0, 0), (0, 0, 1), (0, 1, 1), (0, 1, 0)), 0.7),
    ((0, 1, 0), ((0, 1, 0), (1, 1, 0), (1, 1, 1), (0, 1, 1)), 1.08),
    ((0, -1, 0), ((0, 0, 0), (1, 0, 0), (1, 0, 1), (0, 0, 1)), 0.45),
    ((0, 0, 1), ((0, 0, 1), (1, 0, 1), (1, 1, 1), (0, 1, 1)), 0.58),
    ((0, 0, -1), ((0, 0, 0), (1, 0, 0), (1, 1, 0), (0, 1, 0)), 0.64),
)


def camera_basis(yaw, pitch):
    """Minecraft angles: yaw 0 looks south (+z), 90 west; pitch 90 looks down."""
    yaw, pitch = math.radians(yaw), math.radians(pitch)
    forward = np.array([-math.sin(yaw) * math.cos(pitch), -math.sin(pitch), math.cos(yaw) * math.cos(pitch)])
    right = np.array([-math.cos(yaw), 0.0, -math.sin(yaw)])
    return forward, right, np.cross(right, forward)


def normalize_camera(camera, dimensions):
    """Validate a planner camera; the eye may lie outside the volume within a margin."""
    if not isinstance(camera, dict) or not {"position", "yaw", "pitch"} <= set(camera) <= {"position", "yaw", "pitch", "fov"}:
        raise ValueError("camera requires position, yaw and pitch, with optional fov")
    def number(value, name, low, high):
        if type(value) not in (int, float) or not math.isfinite(value) or not low <= value <= high:
            raise ValueError(f"camera {name} must be a number from {low} to {high}")
        return float(value)
    position = camera["position"]
    if not isinstance(position, list) or len(position) != 3:
        raise ValueError("camera position must be three local xyz numbers")
    position = [number(v, "position", -MAX_CAMERA_DISTANCE, size + MAX_CAMERA_DISTANCE)
                for v, size in zip(position, dimensions)]
    return {"position": position, "yaw": number(camera["yaw"], "yaw", -360, 360),
            "pitch": number(camera["pitch"], "pitch", -90, 90),
            "fov": number(camera.get("fov", DEFAULT_CAMERA_FOV), "fov", MIN_CAMERA_FOV, MAX_CAMERA_FOV)}


def render_camera(volume, path, camera, width=PREVIEW_WIDTH, height=PREVIEW_HEIGHT, report=None):
    """Perspective cube preview from a chosen eye point (see ``normalize_camera``).

    Faces are culled against solid neighbours and drawn far to near; faces
    crossing the eye plane are skipped rather than clipped.
    """
    camera = normalize_camera(camera, volume.dimensions)
    eye = np.array(camera["position"])
    forward, right, up = camera_basis(camera["yaw"], camera["pitch"])
    focal = (width / 2) / math.tan(math.radians(camera["fov"]) / 2)
    cx, cy = width / 2, height / 2
    original = volume.blocks
    solid_table = np.array([parse_state(s)[0] not in AIR for s in volume.palette])
    stride = max(1, math.ceil(max(original.shape) / 120))
    while True:
        solid = solid_table[original[::stride, ::stride, ::stride]]
        padded = np.pad(solid, 1)
        # Arrays are (y, z, x); a face is exposed when its neighbour is not solid.
        neighbours = {(1, 0, 0): padded[1:-1, 1:-1, 2:], (-1, 0, 0): padded[1:-1, 1:-1, :-2],
                      (0, 1, 0): padded[2:, 1:-1, 1:-1], (0, -1, 0): padded[:-2, 1:-1, 1:-1],
                      (0, 0, 1): padded[1:-1, 2:, 1:-1], (0, 0, -1): padded[1:-1, :-2, 1:-1]}
        exposed = {normal: solid & ~beside for normal, beside in neighbours.items()}
        surface = np.logical_or.reduce(list(exposed.values()))
        if int(surface.sum()) <= 45_000 or stride >= max(original.shape):
            break
        stride += 1
    blocks = original[::stride, ::stride, ::stride]
    cells = np.argwhere(surface)  # (y, z, x)
    xyz = cells[:, [2, 0, 1]].astype(float)
    corners, shades, owners = [], [], []
    for normal, offsets, shade in _FACES:
        visible = exposed[normal][cells[:, 0], cells[:, 1], cells[:, 2]]
        axis = int(np.flatnonzero(normal)[0])
        plane = (xyz[:, axis] + (normal[axis] > 0)) * stride
        visible &= (eye[axis] > plane) if normal[axis] > 0 else (eye[axis] < plane)
        index = np.flatnonzero(visible)
        corners.append((xyz[index, None, :] + np.array(offsets, dtype=float)[None]) * stride)
        shades.append(np.full(len(index), shade))
        owners.append(index)
    corners = np.concatenate(corners) if corners else np.empty((0, 4, 3))
    shades, owners = np.concatenate(shades), np.concatenate(owners)
    relative = corners - eye
    depth = relative @ forward
    keep = (depth > 0.05).all(axis=1)
    sx = cx + focal * (relative @ right) / np.where(depth > 0.05, depth, 1)
    sy = cy - focal * (relative @ up) / np.where(depth > 0.05, depth, 1)
    keep &= ~((sx.max(axis=1) < 0) | (sx.min(axis=1) > width) | (sy.max(axis=1) < 0) | (sy.min(axis=1) > height))
    centers = (xyz[owners] + 0.5) * stride
    distance = np.linalg.norm(centers - eye, axis=1)
    order = [i for i in np.argsort(-distance, kind="stable") if keep[i]]
    image = Image.new("RGB", (width, height), (15, 23, 31))
    draw = ImageDraw.Draw(image)
    # Outline the selection's floor so empty or distant volumes stay oriented.
    w, h, l = volume.dimensions
    floor = [(0, 0, 0), (w, 0, 0), (w, 0, l), (0, 0, l)]
    for a, b in zip(floor, floor[1:] + floor[:1]):
        ends = np.array([a, b], dtype=float) - eye
        if (ends @ forward > 0.05).all():
            points = [(cx + focal * (e @ right) / (e @ forward), cy - focal * (e @ up) / (e @ forward)) for e in ends]
            draw.line(points, fill=(49, 68, 73), width=2)
    colors = [block_color(s) for s in volume.palette]
    for i in order:
        y, z, x = cells[owners[i]]
        color = colors[int(blocks[y, z, x])]
        draw.polygon(list(zip(sx[i].tolist(), sy[i].tolist())),
                     fill=tuple(min(255, int(c * shades[i])) for c in color))
    # Orientation gizmo: projected world axes, dimmed when pointing away.
    gx, gy = width - 80, height - 80
    placed = []
    for vector, text in (((0, 1, 0), "+Y up"), ((1, 0, 0), "+X E"), ((0, 0, 1), "+Z S")):
        vector = np.array(vector, dtype=float)
        dx, dy = float(vector @ right), -float(vector @ up)
        toward = vector @ forward <= 0
        shade = (178, 220, 205) if toward else (96, 118, 112)
        draw.line([(gx, gy), (gx + 34 * dx, gy + 34 * dy)], fill=shade, width=2)
        size = math.hypot(dx, dy)
        if size < 0.2:
            # Nearly along the view: say so rather than labelling a dot.
            text += " (toward you)" if toward else " (away)"
            x, y = gx, gy + 44
        else:
            x, y = gx + (34 + 16 / size) * dx, gy + (34 + 16 / size) * dy
        while any(abs(x - px) < 48 and abs(y - py) < 14 for px, py in placed):
            y += 14 if dy >= 0 else -14
        placed.append((x, y))
        draw.text((x, y), text, anchor="mm", font=load_font(12), fill=shade)
    px, py, pz = (round(v, 1) for v in camera["position"])
    draw.text((16, 12), f"WorldPrompt camera · eye ({px}, {py}, {pz}) · yaw {camera['yaw']:g} · pitch {camera['pitch']:g} · fov {camera['fov']:g}",
              font=load_font(15), fill=(178, 220, 205))
    detail = f"{w} × {h} × {l} blocks · X east / Y up / Z south · cube preview"
    if stride > 1:
        detail += f" · sampled every {stride} blocks"
    draw.text((16, height - 26), detail, font=load_font(13), fill=(113, 134, 145))
    if not order:
        draw.text((cx, cy), "No blocks in view", anchor="mm", font=load_font(20), fill=(170, 191, 198))
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    image.save(path, "PNG")
    if report is not None:
        report.update(stride=stride, faces_drawn=len(order), camera=camera,
                      forward=[round(float(v), 4) for v in forward])
    return path


def render_slice(volume, path, axis="y", index=None):
    axes = {"x": 2, "y": 0, "z": 1}
    if axis not in axes:
        raise VolumeError("Slice axis must be x, y or z")
    dimension = volume.blocks.shape[axes[axis]]
    index = dimension // 2 if index is None else index
    if type(index) is not int or not 0 <= index < dimension:
        raise VolumeError("Slice index is outside the volume")
    grid = np.take(volume.blocks, index, axis=axes[axis])
    if axis != "y":
        grid = np.flipud(grid)
    colors = np.array([block_color(s) for s in volume.palette], dtype=np.uint8)
    image = Image.fromarray(colors[grid])
    scale = max(1, min(24, 960 // max(grid.shape)))
    image = image.resize((grid.shape[1] * scale, grid.shape[0] * scale), Image.Resampling.NEAREST)
    result = Image.new("RGB", (max(image.width + 40, 460), image.height + 85), (15, 23, 31))
    result.paste(image, (20, 55))
    ImageDraw.Draw(result).text((20, 18), f"Slice {axis.upper()} = {index} · coordinates in blocks", font=load_font(17), fill=(178, 220, 205))
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    result.save(path, "PNG")
    return path


def render_top(volume, path, max_size=PREVIEW_WIDTH):
    """Render each column's highest non-air block; X right and Z down."""
    solid = volume.non_air_mask()
    h, l, w = volume.blocks.shape
    occupied = solid.any(axis=0)
    heights = h - 1 - np.argmax(solid[::-1], axis=0)
    zz, xx = np.indices((l, w))
    palette_ids = volume.blocks[heights, zz, xx]
    colors = np.array([block_color(s) for s in volume.palette], dtype=np.uint8)
    rgb = colors[palette_ids].astype(float)
    rgb *= (0.75 + 0.25 * heights / max(h - 1, 1))[:, :, None]
    rgb[~occupied] = (15, 23, 31)
    image = Image.fromarray(rgb.astype(np.uint8))
    scale = min(24, (max_size - 40) / max(w, l))
    image = image.resize((max(1, round(w * scale)), max(1, round(l * scale))), Image.Resampling.NEAREST)
    result = Image.new("RGB", (max(image.width + 40, 460), image.height + 92), (15, 23, 31))
    result.paste(image, (20, 54))
    draw = ImageDraw.Draw(result)
    draw.text((20, 17), "Top view · X east → / Z south ↓", font=load_font(17), fill=(178, 220, 205))
    draw.text((20, image.height + 64), "Highest occupied block per column · brighter is higher", font=load_font(12), fill=(113, 134, 145))
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    result.save(path, "PNG")
    return path
