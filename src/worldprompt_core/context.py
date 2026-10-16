"""Optional, bounded adapter context; never changes the editable volume."""
from __future__ import annotations

import math
import re

from .limits import MAX_WORLD_COORDINATE

# A viewpoint may lie outside the world border, so its positions get twice the room.
MAX_VIEWPOINT_COORDINATE = 2 * MAX_WORLD_COORDINATE


def normalize_viewpoint(value):
    """Convert a submit-time Minecraft viewpoint to the core's local axes.

    A player may stand outside the selection. Position and camera direction are
    weak reference cues, not an instruction to edit an unseen surrounding world.
    """
    if value is None:
        return None
    fields = {"position", "eye_position", "yaw", "pitch", "look_direction", "selection_origin", "dimension"}
    if not isinstance(value, dict) or set(value) != fields:
        raise ValueError("Viewpoint requires position, eye_position, yaw, pitch, look_direction, selection_origin and dimension.")

    def number(item, name, maximum=MAX_VIEWPOINT_COORDINATE):
        if type(item) not in (int, float) or abs(item) > maximum or not math.isfinite(item):
            raise ValueError(f"Viewpoint {name} must contain finite bounded numbers.")
        return float(item)

    def vector(item, name, maximum=MAX_VIEWPOINT_COORDINATE):
        if not isinstance(item, list) or len(item) != 3:
            raise ValueError(f"Viewpoint {name} must contain three coordinates.")
        return [number(n, name, maximum) for n in item]

    position = vector(value["position"], "position")
    eye = vector(value["eye_position"], "eye_position")
    origin = value["selection_origin"]
    if (not isinstance(origin, list) or len(origin) != 3 or
            any(type(n) is not int or abs(n) > MAX_WORLD_COORDINATE for n in origin)):
        raise ValueError("Viewpoint selection_origin must contain three bounded integer world coordinates.")
    direction = vector(value["look_direction"], "look_direction", maximum=1.0001)
    magnitude = math.sqrt(sum(n * n for n in direction))
    if abs(magnitude - 1) > 0.001:
        raise ValueError("Viewpoint look_direction must be a unit vector.")
    yaw = number(value["yaw"], "yaw")
    pitch = number(value["pitch"], "pitch", maximum=90)
    dimension = value["dimension"]
    if not isinstance(dimension, str) or len(dimension) > 256 or not re.fullmatch(r"[a-z0-9_.-]+:[a-z0-9_./-]+", dimension):
        raise ValueError("Viewpoint dimension must be a namespaced dimension ID.")
    return {"position_local": [p - o for p, o in zip(position, origin)],
            "eye_position_local": [p - o for p, o in zip(eye, origin)],
            "world_position": position, "world_eye_position": eye,
            "look_direction": [n / magnitude for n in direction],
            "yaw_degrees": (yaw + 180) % 360 - 180, "pitch_degrees": pitch,
            "selection_origin": list(origin), "dimension": dimension}
