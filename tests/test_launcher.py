"""Desktop reopening must target a live Core using the same settings file."""
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from worldprompt_core.__main__ import reopen_running_core


@pytest.mark.parametrize('access', [
    {}, [], None, {'port': 8765}, {'session': None}, {'session': 123},
    {'session': ''}, {'session': 'bad\nsession'}, {'session': 'é'},
])
def test_malformed_access_file_does_not_prevent_startup(tmp_path, monkeypatch, access):
    settings = tmp_path / 'config.json'
    settings.with_name(settings.name + '.gui-access.json').write_text(json.dumps(access))
    monkeypatch.setattr('urllib.request.build_opener', lambda *args: pytest.fail('Invalid session must not be sent'))
    assert reopen_running_core({'port': 8765}, settings) is False


@pytest.mark.parametrize('case,expected', [
    ('matching', True), ('headless', True), ('other_config', False),
    ('other_product', False), ('stopping', False), ('invalid_json', False),
])
def test_reopen_existing_instance(tmp_path, monkeypatch, case, expected):
    settings = tmp_path / 'config.json'
    access_file = settings.with_name(settings.name + '.gui-access.json')
    state = {'product': 'WorldPrompt Core', 'config_path': str(settings),
             'service': {'stopping': False}}
    if case == 'other_config':
        state['config_path'] = str(tmp_path / 'other.json')
    if case == 'other_product':
        state['product'] = 'Something else'
    if case == 'stopping':
        state['service']['stopping'] = True
    opened = []
    monkeypatch.setattr('webbrowser.open', opened.append)

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            assert self.path == '/api/state'
            assert self.headers['X-WorldPrompt-Session'] == 'fixture-private-session'
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'broken' if case == 'invalid_json' else json.dumps(state).encode())

        def log_message(self, *_):
            pass

    with ThreadingHTTPServer(('127.0.0.1', 0), Handler) as server:
        access_file.write_text(json.dumps({'session': 'fixture-private-session', 'port': server.server_port}))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            assert reopen_running_core({'port': 1}, settings,
                                       open_browser=case != 'headless') is expected
            assert opened == ([f'http://127.0.0.1:{server.server_port}/#session=fixture-private-session']
                              if case == 'matching' else [])
        finally:
            server.shutdown()
            thread.join()
