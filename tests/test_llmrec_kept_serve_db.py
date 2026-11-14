"""A real keeper's model stream and record survive planned control-plane replacement."""

import asyncio
from contextlib import contextmanager
import threading
from types import SimpleNamespace

import pytest

from evals.prompt_regression.runner import running_server
from looper.agents import keeper
from looper.agents.llmrec import STATE_FILE
from looper.agents.llmrec.control import ProxyControl
from tests.db_support import DSN
from tests.fake_model_api import FakeModelAPI, ResponseScript
from tests.test_drain_serve_db import _completed, _project, _query, _task, _until
from tests.test_kept_detach_serve_db import _drained_turns, _stop
from tests import test_kept_run_serve_db as kept
from tests.test_llmrec_serve_db import _health, _install_stubs, _state, cli_entry as cli_entry
from tests.test_llmrec_worker_db import KEY, _EVENTS

pytestmark = pytest.mark.skipif(not DSN, reason="requires disposable LOOPER_TEST_DATABASE_URL")


@contextmanager
def _held_upstream():
    ready = threading.Event()
    values = {}

    async def run():
        values['loop'] = asyncio.get_running_loop()
        gate, stopped = asyncio.Event(), asyncio.Event()
        # FakeModelAPI's gate holds before a Responses completion envelope; the Anthropic parser ignores it.
        script = ResponseScript(events=[*_EVENTS[:-1], {'type': 'response.completed', 'response': {}}, _EVENTS[-1]],
                                gate=gate)
        values.update(gate=gate, stopped=stopped, reached=script.gate_reached)
        try:
            async with FakeModelAPI([script], api_key=KEY) as upstream:
                values['base_url'] = upstream.base_url.removesuffix('/v1') + '/anthropic'
                ready.set()
                await stopped.wait()
        finally:
            ready.set()

    thread = threading.Thread(target=lambda: asyncio.run(run()), name='recording-test-upstream')
    thread.start()
    try:
        assert ready.wait(10) and 'base_url' in values
        yield SimpleNamespace(base_url=values['base_url'], reached=values['reached'],
            release=lambda: values['loop'].call_soon_threadsafe(values['gate'].set))
    finally:
        if 'loop' in values and not values['loop'].is_closed():
            values['loop'].call_soon_threadsafe(values['gate'].set)
            values['loop'].call_soon_threadsafe(values['stopped'].set)
        thread.join(15)
        assert not thread.is_alive(), 'the test upstream thread did not stop'


def test_kept_worker_request_finishes_on_same_record_after_planned_restart(monkeypatch, cli_entry):
    marker = '    log("worker", step=label, pid=os.getpid(), args=args)'
    exchange = '''
    import urllib.request
    request_body = json.dumps({"model": "haiku", "system": [{"type": "text", "text": "test system"}],
        "tools": [], "messages": [{"role": "user", "content": "hello"}], "stream": True}).encode()
    request = urllib.request.Request(os.environ["ANTHROPIC_BASE_URL"].rstrip("/") + "/v1/messages",
        request_body, {"content-type": "application/json", "authorization": "Bearer " + os.environ["ANTHROPIC_AUTH_TOKEN"]})
    with urllib.request.urlopen(request, timeout=90) as response:
        response.read()
'''
    assert kept.CLI.count(marker) == 1
    monkeypatch.setattr(kept, 'CLI', kept.CLI.replace(marker, marker + exchange))
    with _held_upstream() as upstream, kept.fixture() as (root, url, state, server):
        bin_dir, home, runtime = server['bin_dir'], server['home'], server['runtime_root']
        claude = (bin_dir / 'claude').read_bytes()
        _install_stubs(bin_dir)
        (bin_dir / 'claude').write_bytes(claude)
        server['env'] = {'CLAUDE_CONFIG_DIR': str(home / '.claude'), 'CODEX_HOME': str(home / '.codex'),
            'GROK_HOME': str(home / '.grok'), 'CURSOR_CONFIG_DIR': str(home / '.cursor-config'),
            'CURSOR_DATA_DIR': str(home / '.cursor-data'), 'LOOPER_TELEGRAM_BOT_TOKEN': ''}
        server['startup_timeout'] = 120
        state_path = runtime / STATE_FILE
        proxy_state = None
        try:
            with running_server(**server, log_path=root / 'before.log') as api:
                provider = api.post('/api/v1/provider_accounts/create', {'name': 'local-model', 'vendor_type': 'custom',
                    'api_key': KEY, 'endpoints': [{'protocol': 'anthropic-messages',
                                                  'base_url': upstream.base_url}]})['result']['provider']['provider_id']
                model = api.post('/api/v1/ops/models/create', {'provider': provider, 'name': 'haiku',
                    'protocol': 'anthropic-messages'})['result']['model']['model_id']
                profile = api.post('/api/v1/profiles/create', {'name': 'local-worker', 'runtime_id': 'rt-claude-code',
                    'model_id': model})['result']['profile']['profile_id']
                api.post('/api/v1/agent_settings/update_role', {'role': 'worker', 'values': {'profile_id': profile}})
                api.post('/api/v1/llmrec_settings/update', {'enabled': True})
                proxy_state = _until(lambda: (value := _state(state_path)) and _health(value) and value,
                                     'the independent recording proxy', seconds=20)
                task = _task(api, _project(api, root, 'recorded'), 'held')
                _until(upstream.reached.is_set, 'the model response held in the upstream stub')
                directory, before = next(iter(kept._running(runtime).values()))
                records = kept._records(url, task)
                assert len(records) == 1 and records[0]['status'] == 'running'
                record_id = records[0]['id']
                _drained_turns(api)
                _stop(url)
            assert kept._live(before['child_pid']) and kept._live(before['keeper_pid'])
            assert _health(proxy_state)

            with running_server(**server, log_path=root / 'after.log') as api:
                status = _until(lambda: (value := api.post('/api/v1/system/status', {})['result']['keepers'])
                                and value['attached'] == 1 and value, 'the real keeper being adopted')
                assert status['candidates_at_start'] == 1
                current = keeper.read_json(directory / keeper.STATE)
                assert current['child_pid'] == before['child_pid']
                assert _state(state_path)['pid'] == proxy_state['pid']
                assert kept._records(url, task)[0]['id'] == record_id
                (state / 'release-s1').touch()
                upstream.release()
                _completed(api, task)
                requests = _until(lambda: _query(url, 'SELECT record_id, outcome, status FROM llm_requests'),
                                  'the held request being committed')
                assert requests == [{'record_id': record_id, 'outcome': 'completed', 'status': 200}]
                records = kept._records(url, task)
                assert len(records) == 1 and records[0]['id'] == record_id and records[0]['status'] == 'completed'
                assert 'adopt run=' in (root / 'after.log').read_text()
        finally:
            upstream.release()
            async def cleanup():
                owner = ProxyControl(runtime, url, enabled=False)
                current = owner._state()
                if current:
                    await owner._stop(current)
                elif proxy_state and await owner._running(proxy_state):
                    await owner._stop(proxy_state)
            asyncio.run(cleanup())
