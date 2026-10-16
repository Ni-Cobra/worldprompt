import base64
import io
import json

import numpy as np
import pytest
from PIL import Image

from worldprompt_core.core import BlockVolume
from worldprompt_core.planner_tools import PlannerTools, serve
from worldprompt_core.spatial_tools import write_snapshot


def setup_tools(tmp_path, dimensions=(8, 8, 8)):
    source = BlockVolume.empty(dimensions)
    source.blocks[0] = source.palette_id('minecraft:stone')
    source.blocks[3, 2, 1] = source.palette_id('minecraft:oak_stairs[facing=east]')
    write_snapshot(source, tmp_path)
    request = {'volume': source.summary(), 'limits': {'max_operations': 256, 'max_plan_bytes': 131072}}
    (tmp_path / 'request.json').write_text(json.dumps(request))
    return source, PlannerTools(request, tmp_path)


def candidate(x=1):
    return {'version': 1, 'summary': 'Marker', 'operations': [
        {'op': 'fill', 'min': [x, 1, 1], 'max': [x, 1, 1], 'block': 'minecraft:gold_block'}]}


def test_exact_region_order_states_and_lazy_source(tmp_path):
    source, tools = setup_tools(tmp_path)
    assert tools.spatial.source is None
    result = tools.call('inspect_region', {'min': [0, 0, 0], 'max': [2, 4, 3]})
    expected = source.blocks[:5, :4, :3].ravel()
    assert [result['palette'][i] for i in result['blocks']] == [source.palette[i] for i in expected]
    assert not tools.spatial.source.blocks.flags.writeable


@pytest.mark.parametrize('arguments', [
    {'min': [-1, 0, 0], 'max': [1, 1, 1]},
    {'min': [0, 0, 0], 'max': [8, 1, 1]},
    {'min': [2, 0, 0], 'max': [1, 1, 1]},
    {'min': [True, 0, 0], 'max': [1, 1, 1]},
    {'min': [0, 0, 0], 'max': [1, 1, 1], 'target': 'world'},
    {'min': [0, 0, 0], 'max': [1, 1, 1], 'target': 'candidate'},
])
def test_invalid_inspection(tmp_path, arguments):
    _, tools = setup_tools(tmp_path)
    with pytest.raises(ValueError):
        tools.call('inspect_region', arguments)


def test_region_budget_and_unavailable_capture(tmp_path):
    _, tools = setup_tools(tmp_path, (20, 20, 20))
    with pytest.raises(ValueError, match='4096'):
        tools.call('inspect_region', {'min': [0, 0, 0], 'max': [19, 19, 19]})
    assert tools.spatial.source is None
    tools.spatial.directory = None
    with pytest.raises(ValueError, match='unavailable'):
        tools.call('inspect_region', {'min': [0, 0, 0], 'max': [0, 0, 0]})


def test_preview_always_replays_original_and_limits_work(tmp_path):
    source, tools = setup_tools(tmp_path)
    original = source.blocks.copy()
    first = tools.call('preview_plan', {'plan': candidate()})
    assert first['fit_diagnostics']['changed_voxels'] == 1
    assert '_image' not in first
    second = tools.call('preview_plan', {'plan': candidate(2)})
    assert second['previews_remaining'] == 0
    exact = tools.call('inspect_region', {'min': [1, 1, 1], 'max': [2, 1, 1], 'target': 'candidate'})
    assert [exact['palette'][i] for i in exact['blocks']] == ['minecraft:air', 'minecraft:gold_block']
    np.testing.assert_array_equal(tools.spatial.source.blocks, original)
    with pytest.raises(ValueError, match='budget'):
        tools.call('preview_plan', {'plan': candidate()})
    assert tools.call('validate_plan', {'plan': candidate()})['valid']


def test_invalid_candidate_is_not_executed_or_left_stale(tmp_path):
    _, tools = setup_tools(tmp_path)
    tools.call('preview_plan', {'plan': candidate()})
    invalid = candidate()
    invalid['operations'][0]['block'] = 'minecraft:invented'
    assert not tools.call('preview_plan', {'plan': invalid})['valid']
    assert tools.spatial.candidate is None
    tools.max_plan_bytes = 10
    assert not tools.call('preview_plan', {'plan': candidate()})['valid']
    assert tools.spatial.preview_calls == 1


def test_mcp_preview_image_and_candidate_inspection(tmp_path):
    setup_tools(tmp_path)
    messages = [
        {'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call', 'params': {
            'name': 'preview_plan', 'arguments': {'plan': candidate(), 'include_image': True}}},
        {'jsonrpc': '2.0', 'id': 2, 'method': 'tools/call', 'params': {
            'name': 'inspect_region', 'arguments': {'min': [1, 1, 1], 'max': [1, 1, 1], 'target': 'candidate'}}}]
    output = io.StringIO()
    serve(tmp_path / 'request.json', io.BytesIO(('\n'.join(map(json.dumps, messages)) + '\n').encode()), output)
    preview, region = [json.loads(line)['result'] for line in output.getvalue().splitlines()]
    assert not preview['isError'] and len(preview['content']) == 2
    picture = preview['content'][1]
    assert picture['type'] == 'image' and picture['mimeType'] == 'image/png'
    with Image.open(io.BytesIO(base64.b64decode(picture['data']))) as image:
        assert image.size == (768, 544)
    assert json.loads(region['content'][0]['text'])['palette'] == ['minecraft:gold_block']


def test_region_byte_limit(tmp_path):
    source = BlockVolume.empty((16, 16, 16))
    source.palette = ['other:' + str(i) + '_' + 'x' * 100 for i in range(4096)]
    source.blocks[:] = np.arange(4096, dtype=np.uint32).reshape(source.blocks.shape)
    write_snapshot(source, tmp_path)
    tools = PlannerTools({'volume': source.summary(), 'limits': {'max_operations': 256}}, tmp_path)
    with pytest.raises(ValueError, match='256 KiB'):
        tools.call('inspect_region', {'min': [0, 0, 0], 'max': [15, 15, 15]})


@pytest.mark.parametrize('effort,phases', [('normal', ['normal']), ('high', ['draft', 'review'])])
def test_pipeline_supplies_original_capture_to_every_real_planner_pass(tmp_path, monkeypatch, effort, phases):
    from types import SimpleNamespace
    from worldprompt_core import pipeline
    source = BlockVolume.empty((8, 8, 8))
    observed = []

    def planner(summary, prompt, directory, config, *, source_volume, phase='normal', **kwargs):
        assert source_volume is source
        assert not source_volume.blocks.any()
        directory.mkdir()
        observed.append(phase)
        return SimpleNamespace(plan=candidate(), usage={}, elapsed_seconds=0, model='fixture')

    monkeypatch.setattr(pipeline, 'generate_plan', planner)
    result = pipeline.plan_volume(source, 'marker', tmp_path / 'job', tmp_path / 'previews',
                                  pipeline.agent_config_for({}, effort), effort)
    assert observed == phases
    assert result.edited.blocks.any() and not source.blocks.any()


def test_sample_surface_reports_exact_column_heights(tmp_path):
    source = BlockVolume.empty((6, 10, 6))
    source.blocks[:3] = source.palette_id('minecraft:stone')            # ground at y=2
    source.blocks[3:5, 1, 1] = source.palette_id('minecraft:water')     # liquid column at x=1,z=1
    source.blocks[6, 2, 2] = source.palette_id('minecraft:oak_leaves')  # canopy, not standable
    source.blocks[8, 3, 3] = source.palette_id('minecraft:stone')       # floating slab overhead
    write_snapshot(source, tmp_path)
    request = {'volume': source.summary(), 'limits': {'max_operations': 512}}
    tools = PlannerTools(request, tmp_path)
    result = tools.call('sample_surface', {'min': [0, 0], 'max': [5, 5]})
    rows = {(row[0], row[1]): row for row in result['columns']}
    assert len(rows) == 36 and result['fields'][2] == 'top_y'
    def block(index):
        return result['palette'][index] if index >= 0 else None
    x, z, top, top_block, ground, ground_block, liquid, air = rows[(0, 0)]
    assert (top, ground, liquid, air) == (2, 2, -1, 7) and block(ground_block) == 'minecraft:stone'
    assert rows[(1, 1)][2] == 4 and block(rows[(1, 1)][3]) == 'minecraft:water'
    assert rows[(1, 1)][4] == 2 and rows[(1, 1)][6] == 4
    assert rows[(2, 2)][2] == 6 and rows[(2, 2)][4] == 2 and rows[(2, 2)][7] == 3
    assert rows[(3, 3)][4] == 8 and rows[(3, 3)][7] == 1
    stepped = tools.call('sample_surface', {'min': [0, 0], 'max': [5, 5], 'step': 2})
    assert [row[:2] for row in stepped['columns']][:3] == [[0, 0], [2, 0], [4, 0]]
    assert len(stepped['columns']) == 9


@pytest.mark.parametrize('arguments', [
    {'min': [0, 0, 0], 'max': [1, 1]}, {'min': [0, 0], 'max': [8, 0]}, {'min': [3, 0], 'max': [1, 1]},
    {'min': [0, 0], 'max': [1, 1], 'step': 0}, {'min': [0, 0], 'max': [1, 1], 'step': 65},
    {'min': [0, 0], 'max': [1, 1], 'target': 'candidate'}, {'min': [0, 0], 'max': [1, 1], 'extra': 1},
])
def test_invalid_surface_sampling(tmp_path, arguments):
    _, tools = setup_tools(tmp_path)
    with pytest.raises((ValueError, TypeError)):
        tools.call('sample_surface', arguments)


def test_surface_column_budget_and_candidate(tmp_path):
    _, tools = setup_tools(tmp_path, (100, 4, 100))
    with pytest.raises(ValueError, match='4096'):
        tools.call('sample_surface', {'min': [0, 0], 'max': [99, 99]})
    assert len(tools.call('sample_surface', {'min': [0, 0], 'max': [99, 99], 'step': 2})['columns']) == 2500
    tools.call('preview_plan', {'plan': candidate()})
    result = tools.call('sample_surface', {'min': [1, 1], 'max': [1, 1], 'target': 'candidate'})
    assert result['columns'][0][2] == 1 and result['palette'][result['columns'][0][3]] == 'minecraft:gold_block'


def test_preview_camera_renders_and_reports_its_view(tmp_path):
    _, tools = setup_tools(tmp_path)
    camera = {'position': [4, 12, -10], 'yaw': 0, 'pitch': 35}
    result = tools.call('preview_plan', {'plan': candidate(), 'camera': camera})
    assert '_image' in result and result['image']['view'] == 'camera'
    assert result['image']['camera'] == {'position': [4.0, 12.0, -10.0], 'yaw': 0.0, 'pitch': 35.0, 'fov': 70.0}
    assert result['image']['faces_drawn'] > 0 and 'perspective from eye' in result['image_description']
    default = tools.call('preview_plan', {'plan': candidate(), 'include_image': True})
    assert default['image'] == {'view': 'isometric', 'sampling_stride': 1}
    assert 'south-east' in default['image_description']


def test_invalid_camera_does_not_spend_a_preview(tmp_path):
    _, tools = setup_tools(tmp_path)
    with pytest.raises(ValueError, match='camera'):
        tools.call('preview_plan', {'plan': candidate(), 'camera': {'position': [0, 0, 0], 'yaw': 0}})
    assert tools.spatial.preview_calls == 0


def test_unexpected_tool_failure_is_an_error_result_not_the_end_of_the_server(tmp_path, monkeypatch):
    from worldprompt_core.spatial_tools import SpatialTools
    setup_tools(tmp_path)
    def explode(self, **arguments):
        raise OSError('/private/path/snapshot.npy vanished')
    monkeypatch.setattr(SpatialTools, 'inspect_region', explode)
    def call(identifier, name, arguments):
        return {'jsonrpc': '2.0', 'id': identifier, 'method': 'tools/call', 'params': {'name': name, 'arguments': arguments}}
    # A tool-level bug (here an I/O error), a malformed plan shape and a normal call, in one session.
    messages = [call(1, 'inspect_region', {'min': [0, 0, 0], 'max': [1, 1, 1]}),
                call(2, 'validate_plan', {'plan': {'operations': 5}}),
                call(3, 'get_block', {'block': 'minecraft:stone'})]
    output = io.StringIO()
    serve(tmp_path / 'request.json', io.BytesIO(('\n'.join(map(json.dumps, messages)) + '\n').encode()), output)
    responses = [json.loads(line) for line in output.getvalue().splitlines()]
    assert [response['id'] for response in responses] == [1, 2, 3]
    failed = responses[0]['result']
    assert failed['isError'] is True
    assert 'OSError' in failed['content'][0]['text'] and '/private/path' not in failed['content'][0]['text']
    assert not responses[2]['result']['isError']
    assert json.loads(responses[2]['result']['content'][0]['text'])['properties'] == {}


def test_failed_calls_stay_counted_against_the_pass_budget(tmp_path, monkeypatch):
    from worldprompt_core.spatial_tools import SpatialTools
    _, tools = setup_tools(tmp_path)
    def explode(self, **arguments):
        raise RuntimeError('boom')
    monkeypatch.setattr(SpatialTools, 'inspect_region', explode)
    for _ in range(32):
        with pytest.raises(RuntimeError):
            tools.call('inspect_region', {'min': [0, 0, 0], 'max': [0, 0, 0]})
    with pytest.raises(ValueError, match='budget'):
        tools.call('get_block', {'block': 'minecraft:stone'})
