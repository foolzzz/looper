"""The public serve entry point exposes the authenticated local Anthropic endpoint."""
import asyncio
import http.client
import json
import os
from pathlib import Path
import sys
import tempfile
from urllib.parse import urlsplit

import pytest

from evals.prompt_regression import runner
from looper.agents import proc_tree
from looper.agents.llmrec import STATE_FILE
from looper.agents.llmrec.control import ProxyControl
from tests.db_support import DSN, fresh_database_without_recording
from tests.runner_support import require_disposable_database

pytestmark = pytest.mark.skipif(not DSN, reason='requires disposable LOOPER_TEST_DATABASE_URL')


def _models(url, key):
    endpoint = urlsplit(url)
    connection = http.client.HTTPConnection(endpoint.hostname, endpoint.port, timeout=5)
    try:
        connection.request('GET', endpoint.path + '/v1/models', headers={'x-api-key': key})
        response = connection.getresponse()
        return response.status, json.loads(response.read())
    finally:
        connection.close()


def test_serve_local_gateway_enable_rotate_and_disable(monkeypatch):
    require_disposable_database(str(DSN), served=False)
    monkeypatch.setattr(runner, '_SERVE', 'import sys\nfrom looper.cli import main\n'
        'sys.exit(main(["serve", "--host", "127.0.0.1", "--port", sys.argv[1], '
        '"--runtime-root", sys.argv[2]]))\n')
    # Short paths also remain valid if keeper mode is enabled by a future default.
    with tempfile.TemporaryDirectory(prefix='gateway.', dir='/tmp') as directory:
        root = Path(directory)
        runtime, home, bin_dir = (root / name for name in ('runtime', 'home', 'bin'))
        home.mkdir()
        bin_dir.mkdir()
        script = (f'#!{sys.executable}\nimport sys\n'
            "if '--version' in sys.argv:\n    print('2.1.285 (test runtime)'); sys.exit(0)\n"
            "if sys.argv[1:] == ['auth', '--help']:\n    print('Usage: claude auth status'); sys.exit(0)\n"
            "if sys.argv[1:3] == ['auth', 'status']:\n    print('{\"loggedIn\": false}'); sys.exit(1)\n"
            'sys.exit(76)\n')
        for name in ('claude', 'codex', 'omp', 'pi', 'dsh', 'grok', 'cursor-agent', 'agent', 'ttadk', 'lark-cli'):
            executable = bin_dir / name
            executable.write_text(script)
            executable.chmod(0o755)
        environment = {'CLAUDE_CONFIG_DIR': str(home / '.claude'), 'CODEX_HOME': str(home / '.codex'),
            'GROK_HOME': str(home / '.grok'), 'CURSOR_CONFIG_DIR': str(home / '.cursor-config'),
            'CURSOR_DATA_DIR': str(home / '.cursor-data'), 'XDG_CONFIG_HOME': str(home / '.config'),
            'XDG_CACHE_HOME': str(home / '.cache'), 'LOOPER_TELEGRAM_BOT_TOKEN': ''}
        with fresh_database_without_recording(str(DSN)) as (url, _):
            require_disposable_database(url, served=True)
            state = None
            try:
                with runner.running_server(database_url=url, runtime_root=runtime, bin_dir=bin_dir, home=home,
                        notes_root=home / 'notes', env=environment, startup_timeout=45,
                        log_path=root / 'serve.log') as api:
                    assert not api.post('/api/v1/llmrec_settings/get', {})['result']['enabled']
                    provider = api.post('/api/v1/provider_accounts/create', {'name': 'Local chat account',
                        'vendor_type': 'custom', 'api_key': 'synthetic-provider-key',
                        'endpoints': [{'protocol': 'openai-chat', 'base_url': 'https://chat.example.invalid/v1'}]})['result']['provider']
                    model = api.post('/api/v1/ops/models/create', {'provider': provider['provider_id'],
                        'protocol': 'openai-chat', 'name': 'chat-model'})['result']['model']
                    api.post('/api/v1/gateway/alias_set', {'alias': 'local-chat', 'model_id': model['model_id']})
                    settings = api.post('/api/v1/gateway/update', {'enabled': True})['result']
                    assert settings['enabled'] and settings['proxy']['state'] == 'running'
                    endpoint = settings['url']
                    state = json.loads((runtime / STATE_FILE).read_text())
                    assert state['pid'] != os.getpid()
                    assert proc_tree._start_time(state['pid']) == state['start_time']
                    key = api.post('/api/v1/gateway/key_reveal', {})['result']['key']
                    key_path = runtime / '.local/llmrec/gateway.key'
                    assert key_path.read_text().strip() == key and key_path.stat().st_mode & 0o777 == 0o600
                    status, models = _models(endpoint, key)
                    assert status == 200 and [model['id'] for model in models['data']] == ['local-chat']
                    new_key = api.post('/api/v1/gateway/key_rotate', {})['result']['key']
                    assert new_key != key and _models(endpoint, key)[0] == 401
                    assert _models(endpoint, new_key)[0] == 200
                    disabled = api.post('/api/v1/gateway/update', {'enabled': False})['result']
                    assert not disabled['enabled'] and _models(endpoint, new_key)[0] == 404
                assert proc_tree._start_time(state['pid']) == state['start_time'], 'proxy should be detached from serve'
            finally:
                async def cleanup():
                    owner = ProxyControl(runtime, url, enabled=False)
                    current = owner._state()
                    if current:
                        await owner._stop(current, grace_s=5)
                asyncio.run(cleanup())
                if state:
                    assert proc_tree._start_time(state['pid']) != state['start_time']
