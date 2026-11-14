"""The public serve entry point starts a gateway lazily for a translated Manager."""
import asyncio
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import threading

import pytest

from evals.prompt_regression.dialogs import _ask
from evals.prompt_regression.runner import running_server
from looper.agents.llmrec.control import ProxyControl
from tests.db_support import DSN, fresh_database_without_recording
from tests.test_llmrec_resident_db import _resident_executable
from tests.test_llmrec_serve_db import _install_stubs, short_root, cli_entry

pytestmark = pytest.mark.skipif(not DSN, reason='requires disposable LOOPER_TEST_DATABASE_URL')


@contextmanager
def chat_upstream():
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            requests.append((self.path, json.loads(self.rfile.read(int(self.headers['Content-Length']))),
                             {name.lower(): value for name, value in self.headers.items()}))
            chunks = [
                {'id': 'chat-test', 'choices': [{'delta': {'content': 'ok'}, 'finish_reason': None}]},
                {'id': 'chat-test', 'choices': [{'delta': {}, 'finish_reason': 'stop'}]},
                {'id': 'chat-test', 'choices': [], 'usage': {'prompt_tokens': 3, 'completion_tokens': 2}},
            ]
            data = ''.join('data: ' + json.dumps(chunk) + '\n\n' for chunk in chunks) + 'data: [DONE]\n\n'
            self.send_response(200)
            self.send_header('Content-Type', 'text/event-stream')
            self.send_header('Content-Length', str(len(data.encode())))
            self.end_headers()
            self.wfile.write(data.encode())

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    try:
        yield f'http://127.0.0.1:{server.server_port}/v1', requests
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        assert not thread.is_alive()


def test_serve_translated_manager_works_without_recording(short_root, cli_entry):
    root = short_root
    home, runtime, bin_dir = (root / name for name in ('home', 'runtime', 'bin'))
    home.mkdir()
    _install_stubs(bin_dir)
    executable = Path(_resident_executable(bin_dir))
    script = executable.read_text().replace('import json, os, sys, urllib.request',
        "import json, os, sys, urllib.request\n"
        "if '--version' in sys.argv:\n    print('2.1.285 (Claude Code)'); sys.exit(0)\n"
        "if '--print' not in sys.argv:\n    sys.exit(1)")
    # Claude Code applies project settings after process env, then explicit --settings last.
    script = script.replace('argv = sys.argv[1:]',
        "argv = sys.argv[1:]\n"
        "project = json.load(open('.claude/settings.json'))\n"
        "setting = argv[argv.index('--settings') + 1]\n"
        "explicit = json.loads(setting) if setting.startswith('{') else json.load(open(setting))\n"
        "os.environ.update(project['env'])\n"
        "os.environ.update(explicit.get('env', {}))\n"
        "assert os.environ.get('ANTHROPIC_API_KEY') == '', 'project API key escaped settings'")
    script = script.replace("with urllib.request.urlopen(req, timeout=10) as response:",
        "if os.environ.get('ANTHROPIC_API_KEY'):\n"
        "        req.add_header('x-api-key', os.environ['ANTHROPIC_API_KEY'])\n"
        "    with urllib.request.urlopen(req, timeout=10) as response:")
    executable.write_text(script)
    stale_settings = runtime / '.local/prompts/claude-settings/stale.json'
    stale_settings.parent.mkdir(parents=True)
    stale_settings.write_text('obsolete credential')
    project_settings = runtime / '.claude' / 'settings.json'
    project_settings.parent.mkdir(parents=True)
    project_settings.write_text(json.dumps({'env': {'ANTHROPIC_AUTH_TOKEN': 'project-token',
                                                   'ANTHROPIC_API_KEY': 'project-key'}}))
    environment = {'CLAUDE_CONFIG_DIR': str(home / '.claude'), 'CODEX_HOME': str(home / '.codex'),
        'GROK_HOME': str(home / '.grok'), 'CURSOR_CONFIG_DIR': str(home / '.cursor-config'),
        'CURSOR_DATA_DIR': str(home / '.cursor-data'), 'LOOPER_TELEGRAM_BOT_TOKEN': ''}
    with fresh_database_without_recording(str(DSN)) as (url, _), chat_upstream() as (upstream, requests):
        try:
            with running_server(database_url=url, runtime_root=runtime, bin_dir=bin_dir, home=home,
                    notes_root=home / 'notes', log_path=root / 'gateway.log', env=environment) as api:
                assert not stale_settings.exists()
                assert not api.post('/api/v1/llmrec_settings/get', {})['result']['enabled']
                provider = api.post('/api/v1/provider_accounts/create', {'name': 'Chat account',
                    'vendor_type': 'custom', 'api_key': 'synthetic-key',
                    'endpoints': [{'protocol': 'openai-chat', 'base_url': upstream}]})['result']['provider']
                model = api.post('/api/v1/ops/models/create', {'provider': provider['provider_id'],
                    'protocol': 'openai-chat', 'name': 'haiku', 'chat_dialect': 'generic'})['result']['model']
                profile = api.post('/api/v1/profiles/create', {'name': 'Chat manager',
                    'runtime_id': 'rt-claude-code', 'model_id': model['model_id']})['result']['profile']
                assert profile['model_path'] == 'translated'
                api.post('/api/v1/agent_settings/update_role', {'role': 'manager',
                    'values': {'profile_id': profile['profile_id']}})
                assert _ask(api, 'gateway-test', 'Reply briefly.', timeout=20)
                assert _ask(api, 'gateway-test', 'Reply again.', timeout=20)
                records = api.post('/api/v1/runs/list', {'agent_id': 'manager'})['result']['records']
                assert len(records) == 2 and all(row['status'] == 'completed' for row in records)
                assert 'synthetic-key' not in json.dumps(records)
                routes = [row['metadata']['llmrec'] for row in records]
                assert all(row['state'] == 'translating' and not row['recording'] for row in routes)
                assert routes[0]['route'] == routes[1]['route']
                route_file = runtime / '.local/llmrec/routes' / (routes[0]['route'] + '.json')
                local_token = json.loads(route_file.read_text())['local_token']
                assert local_token and local_token != 'synthetic-key'
                settings_files = list((runtime / '.local/prompts/claude-settings').glob('*.json'))
                assert settings_files
                assert all(json.loads(path.read_text())['env']['ANTHROPIC_AUTH_TOKEN'] == local_token
                           for path in settings_files)
                assert all(b'synthetic-key' not in path.read_bytes()
                           for path in runtime.rglob('*') if path.is_file())
                assert len(requests) == 2 and all(path == '/v1/chat/completions' for path, _, _ in requests)
                assert all(body['model'] == 'haiku' for _, body, _ in requests)
                assert all(headers.get('authorization') == 'Bearer synthetic-key' for _, _, headers in requests)
                assert all('x-api-key' not in headers for _, _, headers in requests)
                status = api.post('/api/v1/llmrec_settings/get', {})['result']
                assert status['proxy']['state'] == 'running' and status['translate']['requests'] == 2
                assert api.post('/api/v1/llm_requests/list', {})['result']['requests'] == []
        finally:
            async def cleanup():
                owner = ProxyControl(runtime, url, enabled=False)
                state = owner._state()
                if state:
                    await owner._stop(state)
            asyncio.run(cleanup())
