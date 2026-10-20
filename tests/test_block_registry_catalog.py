"""Per-job live block catalogs remain immutable and scoped across worker/tool boundaries."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import threading

import numpy as np
import pytest

from worldprompt_core import limits
from worldprompt_core.block_registry import (catalog_from_wire, current_catalog,
                                             get_block, list_blocks, using_catalog, write_catalog_file)
from worldprompt_core.core import BlockVolume, VolumeError, apply_plan, prepare_plan, resolved_state, validate_plan
from worldprompt_core.planner_tools import server_command
from worldprompt_core.spatial_tools import write_snapshot
from worldprompt_core.agent_types import AgentConfig


_FIXTURE = Path(__file__).parent / 'fixtures' / 'block_catalog.json'


def catalog(version='test-build-a'):
    value = json.loads(_FIXTURE.read_text(encoding='utf-8'))
    value['minecraft_version'] = version
    return value


def block_plan(block='test:color_block[tone=cyan]'):
    return {'version': 1, 'summary': 'catalog fixture', 'operations': [
        {'op': 'fill', 'min': [0, 0, 0], 'max': [0, 0, 0], 'block': block}]}


def test_catalog_validation_freezes_and_canonicalizes_snapshot():
    source = catalog()
    parsed = catalog_from_wire(source)
    source['blocks']['test:color_block']['properties']['tone'].reverse()
    assert parsed.minecraft_version == 'test-build-a'
    assert parsed.blocks['test:color_block'].properties['tone'] == ('amber', 'cyan')
    assert parsed.blocks['test:color_block'].defaults['tone'] == 'amber'
    with pytest.raises(TypeError):
        parsed.blocks['test:color_block'].defaults['tone'] = 'cyan'
    wire = parsed.to_wire()
    wire['blocks']['test:color_block']['defaults']['tone'] = 'cyan'
    assert parsed.blocks['test:color_block'].defaults['tone'] == 'amber'


@pytest.mark.parametrize('mutate', [
    lambda value: value.update(extra=True),
    lambda value: value.update(minecraft_version='  '),
    lambda value: value['blocks']['test:color_block'].update(extra=True),
    lambda value: value['blocks']['test:color_block']['defaults'].update(extra='x'),
    lambda value: value['blocks']['test:color_block']['properties'].update({'tone': []}),
    lambda value: value['blocks']['test:color_block']['properties'].update({'tone': ['amber', 'amber']}),
    lambda value: value['blocks']['test:color_block']['defaults'].update({'tone': 'missing'}),
    lambda value: value['blocks'].update({'Upper:bad': {'properties': {}, 'defaults': {}}}),
    lambda value: value['blocks']['test:color_block']['properties'].update({'bad-name': ['x']}),
    lambda value: value['blocks']['test:color_block']['properties'].update({'tone': ['has space']}),
    lambda value: value['blocks']['test:color_block']['properties'].update({'tone': ['Uppercase']}),
    lambda value: value['blocks']['test:color_block']['properties'].update({'tone': ['has+plus']}),
])
def test_catalog_rejects_invalid_shape_or_values(mutate):
    value = catalog()
    mutate(value)
    with pytest.raises(ValueError):
        catalog_from_wire(value)


def test_catalog_scope_is_nested_resettable_and_thread_isolated():
    first = catalog_from_wire(catalog('first'))
    second = catalog_from_wire({
        'minecraft_version': 'second',
        'blocks': {'other:stone': {'properties': {}, 'defaults': {}}},
    })
    assert current_catalog() is None
    with using_catalog(first):
        assert list_blocks()['minecraft_version'] == 'first'
        with using_catalog(second):
            assert list_blocks()['blocks'] == ['other:stone']
        assert list_blocks()['blocks'][0] == 'mod:beam'

    barrier = threading.Barrier(2)

    def read(scoped):
        with using_catalog(scoped):
            before = list_blocks()
            barrier.wait(timeout=5)
            after = list_blocks()
            return before['minecraft_version'], before['blocks'], after['minecraft_version'], after['blocks']

    with ThreadPoolExecutor(max_workers=2) as pool:
        a, b = [future.result(timeout=10) for future in (pool.submit(read, first), pool.submit(read, second))]
    assert a[0] == a[2] == 'first' and a[1] == a[3]
    assert b[0] == b[2] == 'second' and b[1] == b[3] == ['other:stone']
    assert current_catalog() is None
    assert list_blocks()['minecraft_version'] == '26.3'


def test_dynamic_validation_defaults_and_property_inheritance_are_scoped():
    live = catalog_from_wire(catalog())
    assert not validate_plan(block_plan(), [1, 1, 1])['valid'], 'Without a catalog the legacy fallback stays vanilla-only.'
    with using_catalog(live):
        assert validate_plan(block_plan(), [1, 1, 1])['valid']
        assert not validate_plan(block_plan('test:color_block[tone=violet]'), [1, 1, 1])['valid']
        assert resolved_state('test:color_block') == ('test:color_block', {'tone': 'amber'})
        source = BlockVolume(np.zeros((1, 1, 1), dtype=np.uint32), ['test:color_block'])
        replacement = {'version': 1, 'summary': 'use live defaults', 'operations': [
            {'op': 'replace', 'min': [0, 0, 0], 'max': [0, 0, 0],
             'block': 'test:color_block[tone=cyan]', 'match': ['test:color_block[tone=amber]'],
             'preserve_properties': False}]}
        assert validate_plan(replacement, [1, 1, 1])['valid']
        edited, _ = apply_plan(source, replacement)
        assert edited.palette[int(edited.blocks[0, 0, 0])] == 'test:color_block[tone=cyan]'

        beam = BlockVolume(np.zeros((1, 1, 1), dtype=np.uint32), ['mod:beam[axis=x]'])
        preserve = {'version': 1, 'summary': 'inherit modded properties', 'operations': [
            {'op': 'replace', 'min': [0, 0, 0], 'max': [0, 0, 0], 'block': 'test:painted_beam',
             'match': ['mod:beam'], 'preserve_properties': True}]}
        result, _ = apply_plan(beam, preserve)
        assert 'test:painted_beam[axis=x]' in result.palette


def test_match_states_use_complete_live_catalog_but_keep_legacy_modded_syntax():
    live = catalog_from_wire(catalog())
    for match in ('test:color_block[tone=violet]', 'other:missing'):
        candidate = block_plan()
        candidate['operations'][0].update(op='replace', match=[match], preserve_properties=False)
        with using_catalog(live):
            assert not validate_plan(candidate, [1, 1, 1])['valid']
            with pytest.raises(VolumeError):
                prepare_plan([1, 1, 1], candidate)

    legacy = block_plan('minecraft:stone')
    legacy['operations'][0].update(op='replace', match=['other:source'], preserve_properties=False)
    assert validate_plan(legacy, [1, 1, 1])['valid']
    assert prepare_plan([1, 1, 1], legacy)


def test_catalog_sidecar_is_private_and_bounded(tmp_path):
    path = tmp_path / 'block-catalog.json'
    write_catalog_file(catalog_from_wire(catalog()), path)
    assert json.loads(path.read_text())['minecraft_version'] == 'test-build-a'
    if os.name == 'posix':
        assert path.stat().st_mode & 0o777 == 0o600
    oversized = tmp_path / 'oversized.json'
    with oversized.open('wb') as stream:
        stream.truncate(limits.MAX_CATALOG_BYTES + 1)
    from worldprompt_core.block_registry import read_catalog_file
    with pytest.raises(ValueError, match='16 MiB'):
        read_catalog_file(oversized)


def test_mcp_subprocess_uses_private_live_catalog_for_all_registry_tools(tmp_path):
    wire_catalog = catalog_from_wire(catalog())
    catalog_path = tmp_path / 'block-catalog.json'
    write_catalog_file(wire_catalog, catalog_path)
    source = BlockVolume.empty((1, 1, 1))
    write_snapshot(source, tmp_path)
    request_path = tmp_path / 'request.json'
    model_request = {'volume': {'dimensions': [1, 1, 1]}, 'limits': {'max_operations': 8}}
    request_path.write_text(json.dumps(model_request), encoding='utf-8')
    plan = block_plan()
    calls = [
        {'jsonrpc': '2.0', 'id': 1, 'method': 'tools/list'},
        {'jsonrpc': '2.0', 'id': 2, 'method': 'tools/call', 'params': {'name': 'list_blocks', 'arguments': {}}},
        {'jsonrpc': '2.0', 'id': 3, 'method': 'tools/call', 'params': {'name': 'get_block', 'arguments': {'block': 'test:color_block'}}},
        {'jsonrpc': '2.0', 'id': 4, 'method': 'tools/call', 'params': {'name': 'validate_plan', 'arguments': {'plan': plan}}},
        {'jsonrpc': '2.0', 'id': 5, 'method': 'tools/call', 'params': {'name': 'preview_plan', 'arguments': {'plan': plan}}},
        {'jsonrpc': '2.0', 'id': 6, 'method': 'tools/call',
         'params': {'name': 'get_block', 'arguments': {'block': 'test:rotating_beacon'}}},
    ]
    # server_command runs inside the job's catalog scope, which is what decides the sidecar.
    with using_catalog(wire_catalog):
        command = server_command(tmp_path, AgentConfig())
    assert '--catalog' in command['args'] and str(catalog_path) in command['args']
    import subprocess
    completed = subprocess.run([command['command'], *command['args']],
                               input=''.join(json.dumps(call) + '\n' for call in calls),
                               capture_output=True, text=True, timeout=30, check=True)
    replies = [json.loads(line) for line in completed.stdout.splitlines()]
    listed = replies[0]['result']['tools']
    assert 'test-build-a' in next(tool['description'] for tool in listed if tool['name'] == 'list_blocks')
    list_result = json.loads(replies[1]['result']['content'][0]['text'])
    assert list_result['blocks'] == ['mod:beam', 'test:color_block', 'test:painted_beam', 'test:rotating_beacon']
    get_result = json.loads(replies[2]['result']['content'][0]['text'])
    assert get_result['defaults'] == {'tone': 'amber'}
    assert json.loads(replies[3]['result']['content'][0]['text'])['valid']
    preview = json.loads(replies[4]['result']['content'][0]['text'])
    assert preview['valid'] and preview['replay_target'] == 'ORIGINAL source'
    # The sidecar carries native value order all the way into the separate tool process.
    beacon = json.loads(replies[5]['result']['content'][0]['text'])
    assert beacon['properties']['facing'] == ['north', 'south', 'east', 'west']
    assert beacon['properties']['rotation'] == [str(index) for index in range(16)]
    assert 'block_catalog' not in model_request and 'test:color_block' not in request_path.read_text()


def test_server_command_omits_the_sidecar_only_for_a_genuinely_legacy_request(tmp_path):
    """No live catalog in scope is a legacy/offline request; the tools then use bundled vanilla."""
    command = server_command(tmp_path, AgentConfig())
    assert '--catalog' not in command['args']


def test_server_command_fails_when_a_live_job_has_no_sidecar(tmp_path):
    """A missing required sidecar is a broken job, not evidence that this is a legacy request.

    Without this, the planner tools would silently plan against bundled vanilla while final
    validation used the live catalog.
    """
    with using_catalog(catalog_from_wire(catalog())):
        with pytest.raises(FileNotFoundError, match='no sidecar'):
            server_command(tmp_path, AgentConfig())


def test_property_values_keep_the_servers_native_order():
    """Mycelium block.catalog@2 law C6, on Core's side of the wire.

    Sorting a domain as text is lossy in both directions: it reorders a direction enum,
    and it claims '10' follows '1'. No layer may restore "order" by sorting.
    """
    live = catalog_from_wire(catalog())
    beacon = live.blocks['test:rotating_beacon'].properties
    assert beacon['facing'] == ('north', 'south', 'east', 'west')
    assert beacon['rotation'] == tuple(str(index) for index in range(16))
    with using_catalog(live):
        assert get_block('test:rotating_beacon')['properties']['facing'] == ['north', 'south', 'east', 'west']
        assert get_block('test:rotating_beacon')['properties']['rotation'] == [str(i) for i in range(16)]
    # The sidecar the planner tools read carries the same order.
    assert live.to_wire()['blocks']['test:rotating_beacon']['properties']['facing'] == \
        ['north', 'south', 'east', 'west']


def test_resolved_states_are_cached_per_catalog_not_per_minecraft_version():
    """Two servers can report the same Minecraft version with different defaults and domains."""
    same_version = {
        'minecraft_version': 'test-build-a',
        'blocks': {'test:color_block': {'properties': {'tone': ['amber', 'cyan']}, 'defaults': {'tone': 'cyan'}}},
    }
    first, second = catalog_from_wire(catalog()), catalog_from_wire(same_version)
    assert first.minecraft_version == second.minecraft_version
    with using_catalog(first):
        assert resolved_state('test:color_block') == ('test:color_block', {'tone': 'amber'})
    with using_catalog(second):
        assert resolved_state('test:color_block') == ('test:color_block', {'tone': 'cyan'})
    with using_catalog(first):
        assert resolved_state('test:color_block') == ('test:color_block', {'tone': 'amber'})
    # And with no catalog at all, the bundled fallback is unaffected by either.
    assert resolved_state('minecraft:stone')[0] == 'minecraft:stone'


def test_a_repeated_lookup_is_cached_and_its_result_stays_the_callers_own():
    live = catalog_from_wire(catalog())
    with using_catalog(live):
        first = resolved_state('test:color_block')[1]
        first['tone'] = 'mutated'
        second = resolved_state('test:color_block')[1]
        assert second == {'tone': 'amber'}, 'mutating a returned dict must not poison the cache'
        assert first is not second, 'each caller gets its own dict'
        # The cached value itself is a read-only view, and a repeat lookup is the same object
        # rather than a fresh parse.
        cached = live.resolved_state('test:color_block')[1]
        with pytest.raises(TypeError):
            cached['tone'] = 'mutated'
        assert live.resolved_state('test:color_block')[1] is cached


def test_planner_instructions_agree_with_the_active_registry_from_the_start():
    """The base text must not assert vanilla and then be contradicted by an addendum."""
    from worldprompt_core.agent import INSTRUCTIONS, registry_instructions
    from worldprompt_core.operations import OPERATIONS

    generated = ''.join(spec.guide for spec in OPERATIONS.values())
    base = INSTRUCTIONS + generated
    assert 'vanilla' not in base.lower(), 'the generic instructions may not claim a vanilla-only registry'

    assert 'bundled vanilla Minecraft 26.3' in registry_instructions()
    with using_catalog(catalog_from_wire(catalog('live-build-7'))):
        live = registry_instructions()
    assert 'live Minecraft live-build-7 server catalog' in live
    assert 'authoritative' in live


def test_a_catalogs_cache_does_not_keep_the_catalog_alive():
    """The resolver must not form a reference cycle with its catalog.

    A cycle would hold up to RESOLVED_STATE_CACHE resolved states per retired
    catalog until a cyclic collection, on a Core that serves many jobs.
    """
    import gc
    import weakref

    live = catalog_from_wire(catalog())
    with using_catalog(live):
        resolved_state('test:color_block')
        resolved_state('test:rotating_beacon[rotation=11]')
    reference = weakref.ref(live)
    gc.disable()           # only plain reference counting may free it
    try:
        del live
        assert reference() is None, 'the resolved-state cache still holds its catalog'
    finally:
        gc.enable()
