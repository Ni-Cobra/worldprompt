import io
import json
import subprocess
import sys

import pytest

from worldprompt_core.agent import AgentConfig, AgentError, generate_plan
from worldprompt_core.block_registry import block_properties, get_block, list_blocks
from worldprompt_core.core import BlockVolume, VolumeError, apply_plan, validate_plan
from worldprompt_core.planner_tools import PlannerTools, serve, server_command


def plan(block='minecraft:stone'):
    return {'version': 1, 'summary': 'platform', 'operations': [
        {'op': 'fill', 'min': [0, 0, 0], 'max': [1, 0, 1], 'block': block}]}


def request():
    return {'volume': {'dimensions': [3, 3, 3]}, 'limits': {'max_operations': 256}}


def test_catalog_complete_paginated_and_defaults_valid():
    assert len(block_properties()) == 1286
    found = []
    offset = 0
    while offset is not None:
        page = list_blocks(offset=offset, limit=100)
        found.extend(page['blocks'])
        offset = page['next_offset']
    assert set(found) == set(block_properties())
    assert len(found) == len(set(found))
    for block in found:
        info = get_block(block)
        assert all(v in info['properties'][k] for k, v in info['defaults'].items())
    assert 'minecraft:oak_stairs' in list_blocks('oak_stairs')['blocks']
    assert get_block('minecraft:oak_stairs')['defaults']['facing'] == 'north'


@pytest.mark.parametrize('state,detail', [
    ('minecraft:chain', 'iron_chain'), ('minecraft:stone[axis=y]', 'no property'),
    ('minecraft:furnace[facing=up]', 'accepted values'),
    ('minecraft:oak_stairs[waterlogged=1]', 'accepted values'),
    ('minecraft:oak_log[axis=x,axis=y]', 'Duplicate'),
    ('minecraft:oak_log[axis=x,]', 'Empty'),
    ('minecraft:command_block[facing=sideways]', 'accepted values'), ('mod:invented', 'Unknown'),
])
def test_rejects_bad_states_before_any_execution(state, detail):
    source = BlockVolume.empty((3, 3, 3))
    diagnostics = validate_plan(plan(state), source.dimensions)
    assert not diagnostics['valid']
    assert detail in diagnostics['errors'][0]['message']
    assert diagnostics['errors'][0]['operation'] == 1
    with pytest.raises(VolumeError):
        apply_plan(source, plan(state))
    assert source.palette == ['minecraft:air'] and not source.blocks.any()


def test_valid_partial_states_and_geometry():
    assert validate_plan(plan('minecraft:oak_stairs[facing=east,half=top]'), [3, 3, 3])['valid']
    assert not validate_plan(plan(), [1, 1, 1])['valid']
    candidate = plan()
    candidate['operations'][0]['min'] = [2, 0, 0]
    assert 'min exceeds max' in validate_plan(candidate, [3, 3, 3])['errors'][0]['message']
    candidate['operations'][0]['extra'] = 1
    assert not validate_plan(candidate, [3, 3, 3])['valid']
    assert not validate_plan(plan(), [3, 3, 3], max_plan_bytes=10)['valid']
    assert validate_plan({'version': 1, 'summary': 'Cannot fit', 'operations': []}, [3, 3, 3])['valid']


def test_block_list_validates_every_candidate_and_single_bounds():
    assert validate_plan(plan(['minecraft:stone', 'minecraft:dirt']), [3, 3, 3])['valid']
    invalid = validate_plan(plan(['minecraft:stone', 'minecraft:furnace[facing=up]']), [3, 3, 3])
    assert not invalid['valid']
    assert invalid['errors'][0]['field'] == 'block[1]'
    single = {'version': 1, 'summary': 'marker', 'operations': [
        {'op': 'single', 'at': [2, 2, 2], 'block': ['minecraft:stone', 'minecraft:dirt']}]}
    assert validate_plan(single, [3, 3, 3])['valid']
    single['operations'][0]['at'] = [3, 2, 2]
    assert not validate_plan(single, [3, 3, 3])['valid']


def test_match_states_are_checked_but_modded_source_matches_supported():
    candidate = plan()
    candidate['operations'][0].update(op='replace', match=['minecraft:furnace[facing=up]'], preserve_properties=True)
    assert not validate_plan(candidate, [3, 3, 3])['valid']
    candidate['operations'][0]['match'] = ['other:source']
    assert validate_plan(candidate, [3, 3, 3])['valid']


def test_mcp_protocol_errors_and_call_budget(tmp_path):
    path = tmp_path / 'request.json'
    path.write_text(json.dumps(request()))
    messages = [{'jsonrpc': '2.0', 'id': 1, 'method': 'initialize'},
                {'jsonrpc': '2.0', 'method': 'notifications/initialized'},
                {'jsonrpc': '2.0', 'id': 2, 'method': 'tools/list'},
                {'jsonrpc': '2.0', 'id': 3, 'method': 'tools/call', 'params': {'name': 'validate_plan', 'arguments': {'plan': plan('minecraft:nope')}}},
                {'jsonrpc': '2.0', 'id': 4, 'method': 'tools/call', 'params': {'name': 'get_block', 'arguments': {'block': 'minecraft:stone'}}},
                {'jsonrpc': '2.0', 'id': 5, 'method': 'tools/call', 'params': {'name': 'validate_plan', 'arguments': {'plan': plan()}}}]
    wire = ''.join(json.dumps(m) + '\n' for m in messages)
    command = server_command(tmp_path, AgentConfig())
    result = subprocess.run([command['command'], *command['args']], input=wire, capture_output=True, text=True, timeout=30, check=True)
    responses = [json.loads(line) for line in result.stdout.splitlines()]
    assert len(responses) == 5
    assert {tool['name'] for tool in responses[1]['result']['tools']} == {'list_blocks', 'get_block', 'validate_plan', 'inspect_region', 'sample_surface', 'preview_plan'}
    assert not json.loads(responses[2]['result']['content'][0]['text'])['valid']
    assert json.loads(responses[4]['result']['content'][0]['text'])['valid']
    server = PlannerTools(request())
    for _ in range(32):
        server.call('list_blocks', {'query': 'oak'})
    with pytest.raises(ValueError, match='budget'):
        server.call('list_blocks', {})
    assert serve(path, io.BytesIO(b'x' * (1024 * 1024 + 1)), io.StringIO()) == 1


@pytest.mark.parametrize('provider', ['codex', 'claude'])
def test_provider_can_launch_tools_repair_and_submit(tmp_path, provider):
    fake = tmp_path / 'fake.py'
    fake.write_text('''import json,sys,subprocess
from pathlib import Path
args = sys.argv
if '--mcp-config' in args:
    server = json.loads(args[args.index('--mcp-config')+1])['mcpServers']['worldprompt']
else:
    server = {}
    for i, value in enumerate(args):
        if value == '-c' and args[i+1].startswith('mcp_servers.worldprompt.'):
            key, value = args[i+1].split('=',1)
            server[key.rsplit('.',1)[1]] = json.loads(value)
base = {'version':1,'summary':'Corrected','operations':[{'op':'fill','min':[0,0,0],'max':[1,0,1],'block':'minecraft:furnace[facing=up]'}]}
messages=[]
for i, block in enumerate(['minecraft:furnace[facing=up]', 'minecraft:furnace[facing=east]']):
    base['operations'][0]['block'] = block
    messages.append(json.dumps({'jsonrpc':'2.0','id':i,'method':'tools/call','params':{'name':'validate_plan','arguments':{'plan':base}}}))
p = subprocess.run([server['command'],*server['args']],input='\\n'.join(messages)+'\\n',text=True,capture_output=True,check=True)
results=[json.loads(json.loads(line)['result']['content'][0]['text']) for line in p.stdout.splitlines()]
assert not results[0]['valid'] and results[1]['valid']
if '--mcp-config' in args:
    print(json.dumps({'type':'result','subtype':'success','structured_output':base}),flush=True)
else:
    Path(args[args.index('--output-last-message')+1]).write_text(json.dumps(base))
''')
    result = generate_plan({'dimensions': [3, 3, 3]}, 'platform', tmp_path / 'job',
                           AgentConfig(provider=provider, command=(sys.executable, str(fake)), model='sonnet' if provider == 'claude' else 'gpt-6-astra'))
    assert result.plan['operations'][0]['block'] == 'minecraft:furnace[facing=east]'


def test_final_rejection_preserves_high_draft_repair(tmp_path):
    fake = tmp_path / 'invalid.py'
    fake.write_text("import sys,json; from pathlib import Path; "
                    "Path(sys.argv[sys.argv.index('--output-last-message')+1]).write_text("
                    + repr(json.dumps(plan('minecraft:furnace[facing=up]'))) + ")")
    config = AgentConfig(command=(sys.executable, str(fake)))
    for phase in ['normal', 'review']:
        with pytest.raises(AgentError, match='final plan failed validation'):
            generate_plan({'dimensions': [3, 3, 3]}, 'build', tmp_path / phase, config, phase=phase)
    draft = generate_plan({'dimensions': [3, 3, 3]}, 'build', tmp_path / 'draft', config, phase='draft')
    with pytest.raises(VolumeError):
        apply_plan(BlockVolume.empty((3, 3, 3)), draft.plan)


@pytest.mark.parametrize('block', ['minecraft:command_block[facing=up]', 'minecraft:repeating_command_block',
                                   'minecraft:chain_command_block', 'minecraft:structure_block', 'minecraft:jigsaw'])
def test_technical_blocks_are_placeable_without_data(block):
    source = BlockVolume.empty((3, 3, 3))
    assert validate_plan(plan(block), source.dimensions)['valid']
    edited, _ = apply_plan(source, plan(block))
    assert edited.palette[int(edited.blocks[0, 0, 0])] == block
    assert block.split('[')[0] in list_blocks(block.split(':')[1].split('[')[0])['blocks']
