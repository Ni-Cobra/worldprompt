"""Offline CLI protocol coverage, including image payloads and subscription auth."""
import base64
from dataclasses import replace
import json
import os
import subprocess
import sys
import time
from unittest.mock import patch

from PIL import Image
import pytest

from worldprompt_core.agent import AgentConfig, AgentError, build_command, check_provider, generate_plan
from worldprompt_core.providers import provider_environment
from worldprompt_core.pipeline import agent_config_for


PLAN = {"version": 1, "summary": "Marker", "operations": [
    {"op": "fill", "min": [1, 1, 1], "max": [1, 1, 1], "block": "minecraft:stone"}]}
SUMMARY = {"dimensions": [4, 4, 4], "palette": [{"block": "minecraft:air", "count": 64}]}
FAKE_CLAUDE = r'''
import json,os,pathlib,sys,time,threading
mode=sys.argv[1]
if sys.argv[-2:]==['auth','status']:
    print(json.dumps({'loggedIn':True,'authMethod':'claude.ai','email':'PRIVATE_EMAIL'}))
    sys.exit(0)
if mode=='timeout':time.sleep(60)
request=sys.stdin.buffer.readline().decode("utf-8")
pathlib.Path('captured-input.json').write_text(request,encoding='utf-8')
if mode=='early_eof':
    eof=threading.Event()
    def read_remainder():
        sys.stdin.buffer.read();eof.set()
    threading.Thread(target=read_remainder,daemon=True).start()
    if eof.wait(0.05):sys.exit(0)
if mode=='auth':
    print('authentication failed SECRET_CREDENTIAL',file=sys.stderr)
    sys.exit(1)
if mode=='incompatible':
    print('unknown option --safe-mode SECRET_CREDENTIAL',file=sys.stderr)
    sys.exit(1)
if mode=='logs':
    print('x'*10000)
    sys.exit(0)
plan={'version':1,'summary':'Marker','operations':[{'op':'fill','min':[1,1,1],'max':[1,1,1],'block':'minecraft:stone'}]}
result={'type':'result','subtype':'success','is_error':False,'structured_output':plan,'usage':{'input_tokens':100,'cache_creation_input_tokens':30,'cache_read_input_tokens':20,'output_tokens':50,'invalid':True,'negative':-1}}
if mode=='missing':result.pop('structured_output')
if mode=='invalid':result['structured_output']['operations'][0]['min']=[False,0,0]
if mode=='turns':result.update(subtype='error_max_turns',is_error=True)
if mode=='huge':result['structured_output']['summary']='x'*2000
print(json.dumps({'type':'system','subtype':'init'}))
print(json.dumps(result),flush=True)
if mode=='early_eof':
    if not eof.wait(3):sys.exit(4)
'''


@pytest.fixture
def claude(tmp_path):
    executable = tmp_path / 'fake_claude.py'
    executable.write_text(FAKE_CLAUDE)
    return AgentConfig(provider='claude', command=(sys.executable, str(executable), 'ok'), model='sonnet')


def mode(config, value):
    return replace(config, command=(*config.command[:-1], value))


def test_image_payload_schema_result_and_usage(tmp_path, claude):
    preview = tmp_path / 'preview.png'
    Image.new('RGB', (8, 8), (20, 100, 40)).save(preview)
    job = tmp_path / 'job'
    result = generate_plan(SUMMARY, 'A marker → entrée', job, replace(claude, max_output_tokens=40), [preview])
    assert result.plan == PLAN
    assert result.usage == {'input_tokens': 150, 'cache_creation_input_tokens': 30, 'cache_read_input_tokens': 20, 'output_tokens': 50}
    assert result.output_target_exceeded
    message = json.loads((job / 'captured-input.json').read_text(encoding='utf-8'))
    assert message['type'] == 'user'
    content = message['message']['content']
    assert json.loads(content[0]['text'])['request'] == 'A marker → entrée'
    assert json.loads(content[0]['text'])['volume'] == SUMMARY
    assert content[1]['source']['media_type'] == 'image/png'
    assert base64.b64decode(content[1]['source']['data']) == preview.read_bytes()
    assert json.loads((job / 'plan.json').read_text()) == PLAN
    assert json.loads((job / 'usage.json').read_text())['provider'] == 'claude'


def test_claude_command_retains_login_and_exposes_only_planner_tools(tmp_path, claude):
    command = build_command(tmp_path, claude)
    assert '--restricted' in command
    assert '--safe-mode' not in command
    assert '--bare' not in command
    assert '--dangerously-skip-permissions' not in command
    assert command[command.index('--tools') + 1] == ''
    assert command[command.index('--setting-sources') + 1] == ''
    assert set(json.loads(command[command.index('--mcp-config') + 1])['mcpServers']) == {'worldprompt'}
    assert 'mcp__worldprompt__validate_plan' in command
    assert command[command.index('--permission-mode') + 1] == 'dontAsk'
    assert command[command.index('--max-turns') + 1] == '12'
    assert json.loads(command[command.index('--json-schema') + 1])['required'] == ['version', 'summary', 'operations']
    assert '--no-session-persistence' in command


@pytest.mark.parametrize(('name', 'error'), [
    ('missing', 'no structured plan'), ('invalid', 'invalid min coordinates'),
    ('turns', 'turn limit'), ('huge', 'plan size limit'), ('logs', 'log size limit'),
    ('auth', 'claude auth login'), ('incompatible', 'installed CLI is incompatible')])
def test_claude_errors_are_bounded_and_hide_auth_details(tmp_path, claude, name, error):
    config = replace(mode(claude, name), max_plan_bytes=1024, max_log_bytes=6000)
    with pytest.raises(AgentError, match=error) as caught:
        generate_plan(SUMMARY, 'Marker', tmp_path / name, config)
    assert 'SECRET_CREDENTIAL' not in str(caught.value)


def test_claude_watchdog_reaps_stalled_child(tmp_path, claude):
    processes = []
    popen = subprocess.Popen
    def capture(*args, **kwargs):
        process = popen(*args, **kwargs)
        processes.append(process)
        return process
    started = time.monotonic()
    with patch('worldprompt_core.agent.subprocess.Popen', side_effect=capture), pytest.raises(AgentError, match='exceeded'):
        generate_plan(SUMMARY, 'Marker', tmp_path / 'timeout', replace(mode(claude, 'timeout'), timeout_seconds=0.2))
    assert time.monotonic() - started < 30
    assert processes[0].poll() is not None


def test_subscription_status_is_private(claude):
    status = check_provider(claude)
    assert status['ready'] is True
    assert 'PRIVATE_EMAIL' not in json.dumps(status)


def test_saved_default_discovers_claude_for_status_and_planning(tmp_path, claude, monkeypatch):
    # agent.check_provider and providers.build_command each bind resolve_command.
    discovered = lambda name, command: list(claude.command) if not command else list(command)
    monkeypatch.setattr('worldprompt_core.agent.resolve_command', discovered)
    monkeypatch.setattr('worldprompt_core.providers.resolve_command', discovered)
    saved = replace(claude, command=())
    assert check_provider(saved)['ready']
    assert generate_plan(SUMMARY, 'Marker', tmp_path / 'discovered', saved).plan == PLAN


def test_saved_default_discovers_codex_for_status_and_command(tmp_path, monkeypatch):
    monkeypatch.setattr('worldprompt_core.cli_discovery.discover_executable', lambda name: '/installed/codex')
    response = subprocess.CompletedProcess([], 0, '', 'Logged in using ChatGPT')
    with patch('worldprompt_core.cli_process.run_bounded', return_value=response) as run:
        assert check_provider(AgentConfig())['ready']
        assert run.call_args.args[0] == ['/installed/codex', 'login', 'status']
    assert build_command(tmp_path, AgentConfig())[0] == '/installed/codex'


@pytest.mark.parametrize('auth_method', ['api_key', 'console', None])
def test_claude_api_auth_is_not_supported(claude, auth_method):
    response = subprocess.CompletedProcess([], 0, json.dumps({'loggedIn': True, 'authMethod': auth_method}), '')
    with patch('worldprompt_core.cli_process.run_bounded', return_value=response):
        status = check_provider(claude)
    assert not status['ready']
    assert 'API' in status['message']


def test_codex_api_key_auth_is_not_supported():
    response = subprocess.CompletedProcess([], 0, '', 'Logged in using API key - SECRET')
    with patch('worldprompt_core.cli_process.run_bounded', return_value=response):
        status = check_provider(AgentConfig())
    assert not status['ready']
    assert 'SECRET' not in status['message']


def test_provider_environment_removes_api_routing_and_frozen_libraries(claude):
    environment = {'PATH': '/usr/bin', 'ANTHROPIC_API_KEY': 'secret', 'OPENAI_API_KEY': 'secret',
                   'ANTHROPIC_BASE_URL': 'http://other', 'CLAUDE_CODE_USE_BEDROCK': '1',
                   'CLAUDE_CODE_SIMPLE': '1', 'CLAUDECODE': '1', 'CLAUDE_CONFIG_DIR': '/my-login',
                   'LD_LIBRARY_PATH': '/bundle', 'LD_LIBRARY_PATH_ORIG': '/system'}
    with patch.dict(os.environ, environment, clear=True):
        result = provider_environment(claude)
    assert result['PATH'] == '/usr/bin'
    assert result['CLAUDE_CONFIG_DIR'] == '/my-login'
    assert result['LD_LIBRARY_PATH'] == '/system'
    assert 'LD_LIBRARY_PATH_ORIG' not in result
    assert not any(key in result for key in ('ANTHROPIC_API_KEY', 'OPENAI_API_KEY', 'ANTHROPIC_BASE_URL', 'CLAUDE_CODE_USE_BEDROCK', 'CLAUDE_CODE_SIMPLE', 'CLAUDECODE'))


def test_provider_selection_and_models():
    codex = agent_config_for({'codex_model': 'chosen-codex'})
    assert codex.provider == 'codex'
    assert codex.model == 'chosen-codex'
    claude = agent_config_for({'provider': 'claude', 'claude_command': ['path with spaces/claude.exe'], 'claude_model': 'opus'}, 'high')
    assert claude.provider == 'claude'
    assert claude.command == ('path with spaces/claude.exe',)
    assert claude.model == 'opus'
    assert claude.max_operations == 512
    with pytest.raises(ValueError, match='Only Codex CLI and Claude CLI'):
        agent_config_for({'provider': 'api'})


def test_provider_configuration_rejects_ambiguous_command_and_model_types():
    for value in ['claude --flag', None, 3]:
        with pytest.raises(ValueError, match='argument array'):
            agent_config_for({'provider': 'claude', 'claude_command': value})
    for options in [{'provider': []}, {'model': 12}, {'reasoning_effort': []}]:
        with pytest.raises(ValueError):
            AgentConfig(**options)


def test_claude_cache_usage_is_counted_once_in_shared_ledger(tmp_path, claude):
    from worldprompt_core.usage import token_usage
    result = generate_plan(SUMMARY, 'Marker', tmp_path / 'cached', claude)
    totals = token_usage({'status': 'completed', 'model_calls': 1,
                          'plan_passes': [{'usage': result.usage}]})
    assert totals['input_tokens'] == 150
    assert totals['output_tokens'] == 50
    assert totals['total_tokens'] == 200
    assert totals['complete'] is True


def test_claude_stream_stays_open_until_result_then_sends_eof(tmp_path, claude):
    result = generate_plan(SUMMARY, 'Marker', tmp_path / 'early-eof', mode(claude, 'early_eof'))
    assert result.plan == PLAN


def test_claude_large_stream_timeout_cannot_block_watchdog(tmp_path, claude):
    processes = []
    popen = subprocess.Popen
    def capture(*args, **kwargs):
        child = popen(*args, **kwargs)
        processes.append(child)
        return child
    started = time.monotonic()
    with patch('worldprompt_core.agent.subprocess.Popen', side_effect=capture), pytest.raises(AgentError, match='exceeded'):
        generate_plan(SUMMARY, 'Marker', tmp_path / 'blocked-input',
                      replace(mode(claude, 'timeout'), timeout_seconds=0.2, max_prompt_bytes=512_000),
                      phase='review', context={'draft_plan': 'x' * 300_000})
    assert time.monotonic() - started < 30
    assert processes[0].poll() is not None
    assert processes[0].stdin.closed


GRANDCHILD_CLI = r'''
import pathlib, subprocess, sys, time
mode, marker = sys.argv[1], sys.argv[2]
# The descendant inherits this process's stdout and stderr, so it holds any capture pipe open.
child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], stdout=sys.stdout, stderr=sys.stderr)
pathlib.Path(marker).write_text(str(child.pid))
print("Logged in using ChatGPT", flush=True)
if mode == "hang":
    time.sleep(60)
'''


def stop_descendant(marker):
    from process_checks import process_is_running
    if not marker.exists():
        return
    pid = int(marker.read_text())
    if process_is_running(pid):
        import signal
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass


def wait_until_gone(pid, seconds=10):
    from process_checks import process_is_running
    deadline = time.monotonic() + seconds
    while process_is_running(pid) and time.monotonic() < deadline:
        time.sleep(.05)
    return not process_is_running(pid)


def test_check_provider_is_not_held_by_a_descendant_keeping_the_output_open(tmp_path):
    script, marker = tmp_path / 'grandchild_cli.py', tmp_path / 'pid'
    script.write_text(GRANDCHILD_CLI)
    config = AgentConfig(command=(sys.executable, str(script), 'exit', str(marker)))
    started = time.monotonic()
    try:
        status = check_provider(config)
    finally:
        stop_descendant(marker)
    # Before the fix this waited out the 15 second timeout for the descendant's pipe to close.
    assert time.monotonic() - started < 13
    assert status['ready'] is True


def test_provider_check_timeout_kills_the_whole_process_tree(tmp_path):
    from worldprompt_core.cli_process import run_bounded
    script, marker = tmp_path / 'grandchild_cli.py', tmp_path / 'pid'
    script.write_text(GRANDCHILD_CLI)
    started = time.monotonic()
    try:
        with pytest.raises(subprocess.TimeoutExpired):
            run_bounded([sys.executable, str(script), 'hang', str(marker)], env=dict(os.environ), timeout=3)
        elapsed = time.monotonic() - started
        pid = int(marker.read_text())
        assert wait_until_gone(pid), 'The CLI descendant survived the provider check timeout'
    finally:
        stop_descendant(marker)
    assert elapsed < 30


def test_provider_check_timeout_reports_unavailable_instead_of_hanging(tmp_path, monkeypatch):
    script, marker = tmp_path / 'grandchild_cli.py', tmp_path / 'pid'
    script.write_text(GRANDCHILD_CLI)
    config = AgentConfig(command=(sys.executable, str(script), 'hang', str(marker)))
    from worldprompt_core import cli_process
    real = cli_process.run_bounded
    monkeypatch.setattr(cli_process, 'run_bounded', lambda command, *, env, timeout: real(command, env=env, timeout=2))
    try:
        status = check_provider(config)
    finally:
        stop_descendant(marker)
    assert not status['ready'] and 'TimeoutExpired' in status['message']


def test_every_cli_spawn_runs_inside_the_scoped_dll_directory(tmp_path, claude, monkeypatch):
    from contextlib import contextmanager
    from worldprompt_core import models
    events = []
    @contextmanager
    def scope():
        events.append('enter')
        yield
        events.append('exit')
    monkeypatch.setattr('worldprompt_core.cli_process.windows_cli_dll_directory', scope)
    monkeypatch.setattr('worldprompt_core.agent.cli_process.windows_cli_dll_directory', scope)
    monkeypatch.setattr(models, 'windows_cli_dll_directory', scope)
    assert check_provider(claude)['ready']
    assert events == ['enter', 'exit']
    del events[:]
    generate_plan(SUMMARY, 'Marker', tmp_path / 'job', claude)
    assert events == ['enter', 'exit']
    del events[:]
    with models._connection(claude, 5):
        pass
    assert events == ['enter', 'exit']


def test_scoped_dll_directory_is_a_no_op_off_windows_or_when_not_frozen(monkeypatch):
    from worldprompt_core.cli_process import windows_cli_dll_directory
    with windows_cli_dll_directory():  # Not frozen: must not touch ctypes.windll (absent on Linux).
        pass
    monkeypatch.setattr(sys, 'frozen', True, raising=False)
    if os.name != 'nt':
        with windows_cli_dll_directory():
            pass


@pytest.mark.skipif(os.name != 'nt', reason='SetDllDirectoryW exists only on Windows')
@pytest.mark.parametrize('initial', [r'C:\bundle\_MEI123', None])
def test_frozen_windows_spawns_reset_then_restore_the_dll_directory(tmp_path, claude, monkeypatch, initial):
    import ctypes
    from types import SimpleNamespace
    from worldprompt_core import models
    state, history = {'directory': initial}, []
    def get_directory(size, buffer):
        current = state['directory']
        if not current:
            return 0
        if size < len(current) + 1:
            return len(current) + 1  # Required size including the terminator.
        buffer.value = current
        return len(current)
    def set_directory(value):
        history.append(value)
        state['directory'] = value
        return 1
    kernel = SimpleNamespace(GetDllDirectoryW=get_directory, SetDllDirectoryW=set_directory)
    monkeypatch.setattr(sys, 'frozen', True, raising=False)
    monkeypatch.setattr(ctypes, 'windll', SimpleNamespace(kernel32=kernel), raising=False)
    seen = []
    real_popen = subprocess.Popen
    def watching_popen(*args, **kwargs):
        command = args[0] if args else kwargs.get('args')
        if command[0] != 'taskkill':  # Cleanup helpers are not CLI spawns.
            seen.append(state['directory'])  # What the CLI is created under.
        return real_popen(*args, **kwargs)
    monkeypatch.setattr(subprocess, 'Popen', watching_popen)
    def spawn_checks():
        # GUI-process spawns: sign-in check and model listing.
        assert check_provider(claude)['ready']
        with models._connection(claude, 5):
            pass
        # The planner worker's spawn.
        generate_plan(SUMMARY, 'Marker', tmp_path / f'job{len(seen)}', claude)
    spawn_checks()
    assert seen == [None, None, None]  # Cleared while each CLI starts...
    assert history == [None, initial] * 3  # ...reset, then restored to the saved value (or unset).
    assert state['directory'] == initial  # Nothing is left changed afterwards.


def test_every_configurable_provider_is_registered_with_its_own_sign_in_hint():
    from worldprompt_core.agent_types import PROVIDER_NAMES
    from worldprompt_core.providers import PROVIDERS, get_provider
    assert tuple(PROVIDERS) == PROVIDER_NAMES
    assert get_provider('codex').login_command == 'codex login'
    assert get_provider('claude').login_command == 'claude auth login'
    assert get_provider('claude').keeps_stdin_open and not get_provider('codex').keeps_stdin_open
