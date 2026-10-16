"""CLI model discovery reads only control messages, with bounded output/lifetime."""
import json
from dataclasses import replace
import os
from pathlib import Path
import sys
import time

import pytest

from worldprompt_core.agent import AgentConfig
from worldprompt_core.models import ModelListError, list_models


CLI = r'''
import json, pathlib, subprocess, sys, time
mode, log = sys.argv[1:3]
if mode.startswith('descendant'):
    child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])
    pathlib.Path(log + '.child').write_text(str(child.pid))
    if mode == 'descendant_timeout':
        time.sleep(60)
with open(log, 'w', encoding='utf-8') as calls:
    for line in sys.stdin:
        event = json.loads(line)
        calls.write(json.dumps(event) + '\n'); calls.flush()
        if mode == 'timeout':
            time.sleep(60)
        if mode == 'oversize':
            print('x' * 300000, flush=True); time.sleep(60)
        if mode == 'closed':
            break
        if event.get('method') == 'initialized':
            continue
        if event.get('method') == 'initialize':
            result = {'id': event['id'], 'result': {}}
        elif event.get('method') == 'model/list':
            assert event['params']['includeHidden'] is False
            if mode == 'error':
                result = {'id': event['id'], 'error': {'message': 'private account information'}}
            else:
                cursor = event['params']['cursor']
                efforts = [{'reasoningEffort':'low'}, {'reasoningEffort':'ultra'}, {'reasoningEffort':'Bad!'}, 'high']
                data = ([{'model':'model-a','displayName':'Model A','account':'secret',
                          'supportedReasoningEfforts':efforts,'defaultReasoningEffort':'ultra'},
                         {'model':'text-only','inputModalities':['text']},
                         {'model':'internal','hidden':True}, {'model':[]}, 'bad'] if cursor is None
                        else [{'model':'model-a'}, {'model':'model-b','displayName':'Model B'}])
                result = {'id':event['id'],'result':{'data':data,'nextCursor':'next' if cursor is None or mode == 'cursor' else None}}
        else:
            assert event['type'] == 'control_request' and event['request']['subtype'] == 'initialize'
            result = {'type':'control_response','response':{'request_id':event['request_id'],'subtype':'success',
                      'response':{'account':{'email':'secret'},'models':[{'value':'sonnet','displayName':'Sonnet','resolvedModel':'claude-sonnet-test',
                                  'supportsEffort':True,'supportedEffortLevels':['low','medium','high','max']},
                                 {'value':'opus','displayName':'Opus','supportsEffort':True,'supportedEffortLevels':['low','high','xhigh','max','xhigh']},
                                 {'value':'new-model','displayName':'New model','supportedEffortLevels':['low']}]}}}
        print(json.dumps({'type':'notification'}), flush=True)
        print(json.dumps(result), flush=True)
'''


def config(tmp_path, provider='codex', mode='ok'):
    cli = tmp_path / 'cli.py'
    cli.write_text(CLI)
    return AgentConfig(provider=provider, command=(sys.executable, str(cli), mode, str(tmp_path / 'calls.jsonl')))


def test_codex_pages_are_deduplicated_and_private_fields_are_discarded(tmp_path):
    models = list_models(config(tmp_path))
    assert [m['id'] for m in models] == ['model-a', 'text-only', 'model-b']
    assert models[0]['name'] == 'Model A' and models[0]['supports_images'] is True
    assert models[1]['supports_images'] is False
    assert models[0]['reasoning_efforts'] == ['low', 'ultra'] and models[0]['default_reasoning_effort'] == 'ultra'
    assert models[1]['reasoning_efforts'] == [] and 'default_reasoning_effort' not in models[1]
    assert 'secret' not in json.dumps(models)
    calls = [json.loads(line) for line in (tmp_path / 'calls.jsonl').read_text().splitlines()]
    assert [c['method'] for c in calls] == ['initialize', 'initialized', 'model/list', 'model/list']


def test_claude_reads_initialization_models_without_sending_prompt(tmp_path):
    models = list_models(config(tmp_path, 'claude'))
    assert [m['id'] for m in models] == ['sonnet', 'opus', 'new-model']
    assert models[0]['resolved_model'] == 'claude-sonnet-test'
    # Levels are per model; a model that does not declare effort support offers none.
    assert [m['reasoning_efforts'] for m in models] == [['low', 'medium', 'high', 'max'], ['low', 'high', 'xhigh', 'max'], []]
    assert 'secret' not in json.dumps(models)
    calls = (tmp_path / 'calls.jsonl').read_text().splitlines()
    assert len(calls) == 1 and json.loads(calls[0])['request']['subtype'] == 'initialize'


@pytest.mark.parametrize('provider', ['codex', 'claude'])
def test_model_listing_resolves_saved_default_command(tmp_path, monkeypatch, provider):
    original = config(tmp_path, provider)
    monkeypatch.setattr('worldprompt_core.models.resolve_command', lambda name, command: list(original.command) if not command else list(command))
    saved = replace(original, command=())
    assert list_models(saved)


@pytest.mark.parametrize('mode', ['timeout', 'oversize', 'closed', 'error', 'cursor'])
def test_failed_discovery_is_bounded_and_does_not_expose_cli_errors(tmp_path, mode):
    started = time.monotonic()
    with pytest.raises(ModelListError) as error:
        list_models(config(tmp_path, mode=mode), timeout=.5)
    assert 'private account' not in str(error.value)
    assert time.monotonic() - started < 30


@pytest.mark.skipif(sys.platform != 'linux' and os.name != 'nt', reason='Native Linux/Windows process-tree regression')
@pytest.mark.parametrize('mode', ['descendant_success', 'descendant_timeout'])
def test_discovery_closes_descendant_pipes_on_success_and_timeout(tmp_path, mode):
    started = time.monotonic()
    if mode == 'descendant_timeout':
        with pytest.raises(ModelListError, match='timed out'):
            list_models(config(tmp_path, mode=mode), timeout=.3)
    else:
        assert list_models(config(tmp_path, mode=mode), timeout=.3)
    assert time.monotonic() - started < 30
    pid = int((tmp_path / 'calls.jsonl.child').read_text())
    from process_checks import process_is_running
    def alive():
        if os.name == 'nt':
            return process_is_running(pid)
        status = Path(f'/proc/{pid}/stat')
        return status.exists() and status.read_text().split()[2] != 'Z'
    deadline = time.monotonic() + 5
    while alive():
        assert time.monotonic() < deadline, 'Model-list CLI descendant survived cleanup'
        time.sleep(.01)
