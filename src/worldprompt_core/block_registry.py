"""Offline Minecraft 26.3 block registry: loading, state validation and lookup.

Depends only on ``block_state`` and ``limits``, so the geometry engine (``core``) and the planner
tools can both import it without cycles. Plan-level checks that need geometry live
in ``core.validate_plan``.
"""
import json
from difflib import get_close_matches
from functools import lru_cache
from pathlib import Path

from .block_state import VolumeError, parse_state
from .limits import MAX_BLOCK_PAGE, MAX_BLOCK_QUERY_CHARS

VERSION = '26.3'
_SCHEMAS = Path(__file__).parent / 'schemas'


@lru_cache(maxsize=1)
def block_properties():
    """Actual vanilla 26.3 property domains, exported by the Fabric game test.

    Unknown/modded targets receive no inferred properties. Explicit planned
    targets are checked against this registry before geometry execution.
    """
    return json.loads((_SCHEMAS / f'block-properties-{VERSION}.json').read_text())


@lru_cache(maxsize=1)
def block_defaults():
    """Default value of every property of every vanilla 26.3 block."""
    return json.loads((_SCHEMAS / f'block-defaults-{VERSION}.json').read_text())


def validate_state(state):
    base, properties = parse_state(state)
    registry = block_properties()
    if base not in registry:
        suggestions = ['minecraft:iron_chain'] if base == 'minecraft:chain' else get_close_matches(base, registry, n=3)
        raise VolumeError(f'Unknown Minecraft {VERSION} block {base!r}. Suggestions: {suggestions}')
    for key, value in properties.items():
        if key not in registry[base]:
            raise VolumeError(f'{base} has no property {key!r}; accepted properties: {registry[base]}')
        if value not in registry[base][key]:
            raise VolumeError(f'{base}[{key}={value}]: accepted values for {key}: {registry[base][key]}')
    return base, properties


def list_blocks(query='', offset=0, limit=50):
    if not isinstance(query, str) or len(query) > MAX_BLOCK_QUERY_CHARS:
        raise ValueError(f'query must be a string of at most {MAX_BLOCK_QUERY_CHARS} characters')
    if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= MAX_BLOCK_PAGE:
        raise ValueError(f'offset must be nonnegative and limit must be 1–{MAX_BLOCK_PAGE}')
    names = sorted(name for name in block_properties() if query.lower() in name)
    end = offset + limit
    return {'minecraft_version': VERSION, 'total': len(names), 'blocks': names[offset:end],
            'next_offset': end if end < len(names) else None}


def get_block(block):
    if not isinstance(block, str):
        raise ValueError('block must be a block ID')
    base, _ = validate_state(block)
    return {'minecraft_version': VERSION, 'block': base,
            'properties': block_properties()[base], 'defaults': block_defaults()[base]}
