"""Complete result handoff and independence from retained Core artifacts."""
import base64
import json
import struct

import pytest

from test_world_jobs import submission, supervisor_factory, wait_status
from worldprompt_core import replay
from worldprompt_core.core import BlockVolume


def decode(payload, index):
    return list(struct.iter_unpack('<III', base64.b64decode(payload['operations'][index]['runs_base64'], validate=True)))


def test_downloaded_payload_survives_history_cleanup_and_core_restart(supervisor_factory):
    supervisor = supervisor_factory()
    job = supervisor.submit(submission())
    wait_status(supervisor, job['id'], 'ready')
    payload = json.loads(json.dumps(supervisor.result(job['id'])))
    supervisor.application(job['id'], 'reconstructing', operation=2)
    supervisor.application(job['id'], 'completed', operation=2)
    assert supervisor.remove_jobs([job['id']]) == 1
    supervisor.close()
    restarted = supervisor_factory()
    assert not restarted.jobs
    assert decode(payload, 0) == [(0, 2, 1), (3, 2, 1)]
    assert decode(payload, 1) == [(1, 1, 0)]
    assert payload['plan']['operations'][1]['block'] == 'minecraft:air'


def test_full_result_includes_more_than_one_old_page_and_preserves_order(tmp_path):
    source = BlockVolume.empty((100, 1, 100))
    source.blocks.ravel()[::2] = source.palette_id('minecraft:stone')
    plan = {'version': 1, 'summary': 'Sparse replacement then carve', 'operations': [
        {'op': 'replace', 'min': [0,0,0], 'max': [99,0,99], 'match': ['minecraft:stone'],
         'block': 'minecraft:gold_block', 'preserve_properties': False},
        {'op': 'single', 'at': [0,0,0], 'block': 'minecraft:air'},
        {'op': 'replace', 'min': [0,0,0], 'max': [99,0,99], 'match': ['minecraft:diamond_block'],
         'block': 'minecraft:stone', 'preserve_properties': False}]}
    replay.compile_replay(source, plan, tmp_path / 'replay')
    payload = replay.read_complete_result(tmp_path / 'replay')
    runs = decode(payload, 0)
    assert len(runs) == 5000
    assert runs[0] == (0, 1, 2) and runs[-1] == (9998, 1, 2)
    assert decode(payload, 1) == [(0, 1, 0)]
    assert decode(payload, 2) == []
    assert payload['plan'] == plan


@pytest.mark.parametrize('trailing', [False, True])
def test_complete_result_rejects_corrupt_run_files_before_transfer(tmp_path, trailing):
    plan = {'version': 1, 'summary': 'One block', 'operations': [{'op': 'single', 'at': [0,0,0], 'block': 'minecraft:stone'}]}
    destination = tmp_path / 'replay'
    replay.compile_replay(BlockVolume.empty((1,1,1)), plan, destination)
    path = destination / 'operations/000000.runs'
    path.write_bytes(path.read_bytes() + b'x' if trailing else b'')
    with pytest.raises(ValueError, match='truncated'):
        replay.read_complete_result(destination)


def test_replay_size_budget_applies_before_publishing_manifest(tmp_path, monkeypatch):
    monkeypatch.setattr(replay, 'MAX_REPLAY_BYTES', 12)
    plan = {'version': 1, 'summary': 'Two operations', 'operations': [
        {'op': 'single', 'at': [0,0,0], 'block': 'minecraft:stone'},
        {'op': 'single', 'at': [0,0,0], 'block': 'minecraft:air'}]}
    with pytest.raises(ValueError, match='placement-data limit'):
        replay.compile_replay(BlockVolume.empty((1,1,1)), plan, tmp_path / 'replay')
    assert not (tmp_path / 'replay/manifest.json').exists()
