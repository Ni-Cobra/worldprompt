"""Block-state syntax shared by every module that handles Minecraft block IDs.

A leaf module: it imports only ``limits``, so the registry, the
geometry engine and the planner tools can all depend on it without cycles.
"""
import re

from .limits import MAX_STATE_CHARS

STATE = re.compile(r"^([a-z0-9_.-]+:[a-z0-9_./-]+)(?:\[([a-z0-9_=,.-]+)\])?$")


class VolumeError(ValueError):
    """Input or edit rejected without publishing an output."""


def parse_state(state):
    if not isinstance(state, str) or len(state) > MAX_STATE_CHARS or not (m := STATE.fullmatch(state)):
        raise VolumeError(f"Invalid block state: {state!r}")
    properties = {}
    for pair in (m.group(2) or "").split(","):
        if not pair:
            if m.group(2) is not None:
                raise VolumeError("Empty block property")
            continue
        if pair.count("=") != 1:
            raise VolumeError(f"Invalid block property: {pair!r}")
        key, value = pair.split("=")
        if not key or not value or key in properties:
            raise VolumeError(f"Duplicate or empty block property: {pair!r}")
        properties[key] = value
    return m.group(1), properties


def state_string(base, properties):
    return base + ("[" + ",".join(f"{k}={v}" for k, v in sorted(properties.items())) + "]" if properties else "")
