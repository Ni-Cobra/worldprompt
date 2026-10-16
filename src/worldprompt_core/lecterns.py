"""Prompt books on lecterns: bounded, location-specific parts of the player's request.

The mod reports lecterns holding a book titled or renamed "prompt" inside the
captured selection. Each entry is player-written text tied to a local [x, y, z]
position in plan coordinates. The text is untrusted player content with the same
authority as the chat prompt; it never changes the editable bounds or the rules.
"""
from __future__ import annotations

import re

from .block_state import parse_state
from .limits import MAX_BOOK_CHARACTERS, MAX_LECTERNS, MAX_WORLD_COORDINATE
from .limits import MAX_TOTAL_LECTERN_CHARACTERS as MAX_TOTAL_CHARACTERS

# Newlines separate lines and pages; every other C0/C1 control character and any
# lone surrogate is rejected rather than silently rewritten.
_FORBIDDEN = re.compile(r"[\x00-\x09\x0b-\x1f\x7f-\x9f\ud800-\udfff]")


def validate_lecterns(value, volume, selection_origin=None) -> list[dict]:
    """Return the validated entries, or [] when the optional field is absent.

    Raises ValueError for any malformed entry; nothing is truncated or repaired.
    """
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError("Lecterns must be a list.")
    if len(value) > MAX_LECTERNS:
        raise ValueError(f"At most {MAX_LECTERNS} prompt lecterns are supported; received {len(value)}.")
    width, height, length = volume.dimensions
    seen = set()
    total = 0
    result = []
    for number, item in enumerate(value, 1):
        if not isinstance(item, dict) or not {"position", "text"} <= set(item) <= {"position", "world_position", "text"}:
            raise ValueError(f"Lectern {number} requires position and text, with optional world_position.")
        position = item["position"]
        if (not isinstance(position, list) or len(position) != 3 or any(type(n) is not int for n in position)
                or not all(0 <= n < limit for n, limit in zip(position, (width, height, length)))):
            raise ValueError(f"Lectern {number} position must be local [x, y, z] coordinates inside the selection.")
        if tuple(position) in seen:
            raise ValueError(f"Lectern {number} repeats the position of an earlier lectern.")
        seen.add(tuple(position))
        x, y, z = position
        state = volume.palette[int(volume.blocks[y, z, x])]
        if parse_state(state)[0] != "minecraft:lectern":
            raise ValueError(f"Lectern {number} position {position} is not a lectern in the captured selection.")
        entry = {"position": list(position)}
        if "world_position" in item:
            world = item["world_position"]
            if (not isinstance(world, list) or len(world) != 3
                    or any(type(n) is not int or abs(n) > MAX_WORLD_COORDINATE for n in world)):
                raise ValueError(f"Lectern {number} world_position must contain three bounded integer coordinates.")
            if selection_origin is not None and [o + p for o, p in zip(selection_origin, position)] != world:
                raise ValueError(f"Lectern {number} world_position does not match its local position and the selection origin.")
            entry["world_position"] = list(world)
        text = item["text"]
        if not isinstance(text, str) or not text.strip():
            raise ValueError(f"Lectern {number} text must be a nonempty string.")
        if len(text) > MAX_BOOK_CHARACTERS:
            raise ValueError(f"Lectern {number} text exceeds {MAX_BOOK_CHARACTERS} characters.")
        if _FORBIDDEN.search(text):
            raise ValueError(f"Lectern {number} text contains control characters other than newlines.")
        total += len(text)
        if total > MAX_TOTAL_CHARACTERS:
            raise ValueError(f"Prompt lectern text exceeds {MAX_TOTAL_CHARACTERS} characters in total.")
        entry["text"] = text
        result.append(entry)
    return result


def lectern_check(source, candidate, positions) -> list[dict]:
    """Report whether a candidate left the block at each prompt lectern unchanged."""
    checks = []
    for position in positions:
        x, y, z = position
        before = source.palette[int(source.blocks[y, z, x])]
        entry = {"position": list(position), "source_state": before}
        if candidate is not None:
            after = candidate.palette[int(candidate.blocks[y, z, x])]
            entry.update(candidate_state=after, unchanged_in_candidate=after == before)
        checks.append(entry)
    return checks
