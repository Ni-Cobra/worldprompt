"""Bundled vanilla fallback and validated per-job live block catalogs.

This leaf module depends only on block-state syntax and limits. The active
catalog is scoped with a ContextVar so one worker or tool-server call cannot
change another job's registry view.
"""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
import json
from difflib import get_close_matches
from functools import lru_cache
import os
from pathlib import Path
import re
from types import MappingProxyType
from typing import Mapping
import unicodedata

from .block_state import VolumeError, parse_state
from .limits import (MAX_BLOCK_PAGE, MAX_BLOCK_QUERY_CHARS, MAX_CATALOG_BLOCKS, MAX_CATALOG_BYTES,
                     MAX_CATALOG_PROPERTIES, MAX_CATALOG_PROPERTIES_PER_BLOCK, MAX_CATALOG_VALUES,
                     MAX_CATALOG_VALUES_PER_PROPERTY, MAX_CATALOG_VERSION_CHARS, MAX_STATE_CHARS)

VERSION = '26.3'
_SCHEMAS = Path(__file__).parent / 'schemas'
_BLOCK_ID = re.compile(r'^[a-z0-9_.-]+:[a-z0-9_./-]+$')
_PROPERTY = re.compile(r'^[a-z0-9_]+$')
_PROPERTY_VALUE = re.compile(r'^[a-z0-9_]+$')
#: Resolved states cached per immutable catalog, and for the bundled fallback in ``core``.
RESOLVED_STATE_CACHE = 8192


@dataclass(frozen=True)
class BlockDefinition:
    properties: Mapping[str, tuple[str, ...]]
    defaults: Mapping[str, str]


def _state_resolver(defaults: Mapping[str, Mapping[str, str]]):
    """A bounded resolver for one snapshot's states.

    It closes over that snapshot's defaults and never over the catalog itself, so
    the cache cannot keep its catalog alive through a reference cycle: when the
    catalog is dropped the resolver and its entries go with it.
    """

    @lru_cache(maxsize=RESOLVED_STATE_CACHE)
    def resolve(state: str) -> tuple[str, Mapping[str, str]]:
        base, explicit = parse_state(state)
        return base, MappingProxyType(dict(defaults.get(base, {})) | explicit)

    return resolve


@dataclass(frozen=True)
class BlockCatalog:
    """One immutable snapshot: ``blocks`` is the only stored state.

    ``properties`` and ``defaults`` are views derived from it, so there is one
    source of truth for a block's domains. Each property's values keep the
    order the server captured them in (Mycelium ``block.catalog@2`` law C6);
    only the block and property names are sorted.
    """

    minecraft_version: str
    blocks: Mapping[str, BlockDefinition]

    def __post_init__(self) -> None:
        set_attribute = object.__setattr__
        set_attribute(self, '_properties',
                      MappingProxyType({block: definition.properties for block, definition in self.blocks.items()}))
        defaults = MappingProxyType({block: definition.defaults for block, definition in self.blocks.items()})
        set_attribute(self, '_defaults', defaults)
        # One resolver per snapshot, so a context switch cannot read another server's states.
        # Its values are read-only views: a caller's copy stays theirs.
        set_attribute(self, '_resolve', _state_resolver(defaults))

    @property
    def properties(self) -> Mapping[str, Mapping[str, tuple[str, ...]]]:
        """Block ID to its property domains."""
        return self._properties

    @property
    def defaults(self) -> Mapping[str, Mapping[str, str]]:
        """Block ID to its exact default state."""
        return self._defaults

    def resolved_state(self, state: str) -> tuple[str, Mapping[str, str]]:
        """Resolve omitted properties against this catalog, cached per catalog.

        Returns a read-only mapping; callers that hand it out copy it.
        """
        if not isinstance(state, str):
            parse_state(state)  # Raises VolumeError; unhashable values must not reach the cache.
        return self._resolve(state)

    def to_wire(self) -> dict:
        """Return a canonical detached JSON object for the job and tool sidecar."""
        blocks = {}
        for block in sorted(self.blocks):
            definition = self.blocks[block]
            blocks[block] = {
                'properties': {name: list(definition.properties[name]) for name in sorted(definition.properties)},
                'defaults': {name: definition.defaults[name] for name in sorted(definition.defaults)},
            }
        return {'minecraft_version': self.minecraft_version, 'blocks': blocks}


_ACTIVE_CATALOG: ContextVar[BlockCatalog | None] = ContextVar('worldprompt_block_catalog', default=None)


def catalog_from_wire(value) -> BlockCatalog:
    """Validate and freeze an adapter-supplied block catalog.

    The live map is authoritative when present. It is deliberately validated
    before persistence, and it is never merged with the bundled vanilla files.
    """
    if not isinstance(value, dict) or set(value) != {'minecraft_version', 'blocks'}:
        raise ValueError('block_catalog must contain only minecraft_version and blocks.')
    version = value['minecraft_version']
    if (not isinstance(version, str) or not version.strip() or len(version) > MAX_CATALOG_VERSION_CHARS
            or any(unicodedata.category(character) == 'Cc' for character in version)):
        raise ValueError('block_catalog.minecraft_version must be a nonblank version string.')
    raw_blocks = value['blocks']
    if not isinstance(raw_blocks, dict) or not 1 <= len(raw_blocks) <= MAX_CATALOG_BLOCKS:
        raise ValueError(f'block_catalog.blocks must contain 1–{MAX_CATALOG_BLOCKS} blocks.')
    try:
        encoded_size = len(json.dumps(value, ensure_ascii=False, separators=(',', ':'), allow_nan=False).encode('utf-8'))
    except (TypeError, ValueError, UnicodeError, RecursionError) as exc:
        raise ValueError('block_catalog must be finite UTF-8 JSON data.') from exc
    if encoded_size > MAX_CATALOG_BYTES:
        raise ValueError(f'block_catalog exceeds {MAX_CATALOG_BYTES // (1024 * 1024)} MiB.')

    total_properties = total_values = 0
    frozen_blocks = {}
    for block, raw_definition in raw_blocks.items():
        if not isinstance(block, str) or len(block) > MAX_STATE_CHARS or not _BLOCK_ID.fullmatch(block):
            raise ValueError(f'Invalid canonical block ID in block_catalog: {block!r}.')
        if not isinstance(raw_definition, dict) or set(raw_definition) != {'properties', 'defaults'}:
            raise ValueError(f'{block} must contain only properties and defaults.')
        raw_properties, raw_defaults = raw_definition['properties'], raw_definition['defaults']
        if not isinstance(raw_properties, dict) or not isinstance(raw_defaults, dict):
            raise ValueError(f'{block} properties and defaults must be objects.')
        if len(raw_properties) > MAX_CATALOG_PROPERTIES_PER_BLOCK:
            raise ValueError(f'{block} has too many properties.')
        if set(raw_properties) != set(raw_defaults):
            raise ValueError(f'{block} properties and defaults must have the same names.')
        total_properties += len(raw_properties)
        if total_properties > MAX_CATALOG_PROPERTIES:
            raise ValueError(f'block_catalog exceeds {MAX_CATALOG_PROPERTIES:,} properties.')

        properties = {}
        defaults = {}
        for name, raw_domain in raw_properties.items():
            if not isinstance(name, str) or not _PROPERTY.fullmatch(name):
                raise ValueError(f'{block} has an invalid property name: {name!r}.')
            if (not isinstance(raw_domain, list) or not 1 <= len(raw_domain) <= MAX_CATALOG_VALUES_PER_PROPERTY
                    or any(not _catalog_value(item) for item in raw_domain)
                    or len(raw_domain) != len(set(raw_domain))):
                raise ValueError(f'{block}[{name}] must have 1–{MAX_CATALOG_VALUES_PER_PROPERTY} unique values.')
            total_values += len(raw_domain)
            if total_values > MAX_CATALOG_VALUES:
                raise ValueError(f'block_catalog exceeds {MAX_CATALOG_VALUES:,} property values.')
            default = raw_defaults[name]
            if not _catalog_value(default) or default not in raw_domain:
                raise ValueError(f'{block}[{name}] default must be one of its property values.')
            # The adapter sends the registry's own order; keep it. Duplicates were rejected above.
            properties[name] = tuple(raw_domain)
            defaults[name] = default
        frozen_blocks[block] = BlockDefinition(MappingProxyType(properties), MappingProxyType(defaults))

    return BlockCatalog(version, MappingProxyType(frozen_blocks))


def _catalog_value(value):
    return (isinstance(value, str) and len(value) <= MAX_STATE_CHARS
            and _PROPERTY_VALUE.fullmatch(value) is not None)


def current_catalog() -> BlockCatalog | None:
    """The immutable catalog in this execution context, or None for offline fallback."""
    return _ACTIVE_CATALOG.get()


def catalog_version() -> str:
    """The Minecraft version the active registry describes: the live catalog's, else the bundled one's.

    Every caller that labels registry data for a player or the model uses this, so a live
    job never quotes the bundled version and an offline job never invents a live one.
    """
    catalog = current_catalog()
    return catalog.minecraft_version if catalog is not None else VERSION


@contextmanager
def using_catalog(catalog: BlockCatalog | None):
    """Temporarily bind one validated catalog to this synchronous job/tool scope."""
    if catalog is not None and not isinstance(catalog, BlockCatalog):
        raise TypeError('using_catalog requires a validated BlockCatalog or None.')
    token = _ACTIVE_CATALOG.set(catalog)
    try:
        yield catalog
    finally:
        _ACTIVE_CATALOG.reset(token)


def write_catalog_file(catalog: BlockCatalog, path: str | os.PathLike) -> None:
    """Write a private canonical sidecar for a separately spawned MCP tool server."""
    if not isinstance(catalog, BlockCatalog):
        raise TypeError('A validated BlockCatalog is required.')
    path = Path(path)
    payload = json.dumps(catalog.to_wire(), ensure_ascii=False, separators=(',', ':'), allow_nan=False)
    if len(payload.encode('utf-8')) > MAX_CATALOG_BYTES:
        raise ValueError(f'block_catalog exceeds {MAX_CATALOG_BYTES // (1024 * 1024)} MiB.')
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_BINARY', 0), 0o600)
    with os.fdopen(descriptor, 'w', encoding='utf-8', newline='\n') as stream:
        stream.write(payload)


def read_catalog_file(path: str | os.PathLike) -> BlockCatalog:
    """Load a bounded private MCP sidecar; a supplied but invalid sidecar fails closed."""
    path = Path(path)
    try:
        size = path.stat().st_size
        if size > MAX_CATALOG_BYTES:
            raise ValueError(f'block_catalog exceeds {MAX_CATALOG_BYTES // (1024 * 1024)} MiB.')
        if size == 0:
            raise ValueError('block_catalog sidecar is empty.')
        raw = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f'Could not read the private block_catalog sidecar: {type(exc).__name__}.') from exc
    return catalog_from_wire(raw)


@lru_cache(maxsize=1)
def _bundled_block_properties():
    """Bundled vanilla 26.3 property domains for offline use and legacy requests."""
    return json.loads((_SCHEMAS / f'block-properties-{VERSION}.json').read_text())


@lru_cache(maxsize=1)
def _bundled_block_defaults():
    """Bundled vanilla 26.3 default values for offline use and legacy requests."""
    return json.loads((_SCHEMAS / f'block-defaults-{VERSION}.json').read_text())


def block_properties():
    catalog = current_catalog()
    return catalog.properties if catalog is not None else _bundled_block_properties()


def block_defaults():
    catalog = current_catalog()
    return catalog.defaults if catalog is not None else _bundled_block_defaults()


def validate_state(state):
    base, properties = parse_state(state)
    registry = block_properties()
    if base not in registry:
        suggestions = ['minecraft:iron_chain'] if base == 'minecraft:chain' else get_close_matches(base, registry, n=3)
        raise VolumeError(f'Unknown Minecraft {catalog_version()} block {base!r}. Suggestions: {suggestions}')
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
    return {'minecraft_version': catalog_version(), 'total': len(names), 'blocks': names[offset:end],
            'next_offset': end if end < len(names) else None}


def get_block(block):
    if not isinstance(block, str):
        raise ValueError('block must be a block ID')
    base, _ = validate_state(block)
    return {'minecraft_version': catalog_version(),
            'block': base,
            'properties': {name: list(values) for name, values in block_properties()[base].items()},
            'defaults': dict(block_defaults()[base])}
