"""Installed Workers send real CLI requests only to a synthetic loopback provider."""
from __future__ import annotations

import asyncio
import json
import os
import shutil

import pytest

from tests.db_support import DSN, run_db
from tests.fake_model_api import FakeModelAPI, ResponseScript, completed, event, text_item
from tests.test_profile_registry_db import _ok, _runtime
from tests.test_provider_binding_db import KEY, _profile, _provider, _task, _work

pytestmark = [pytest.mark.installed,
              pytest.mark.skipif(not DSN, reason="requires disposable LOOPER_TEST_DATABASE_URL")]


def _answer():
    text = json.dumps({'status': 'completed', 'summary': 'ok', 'changed_files': {}})
    item = text_item(text)
    return [
        event('response.created', response={'id': 'resp_1', 'object': 'response', 'status': 'in_progress',
                                           'output': []}),
        event('response.output_item.added', output_index=0, item={**item, 'content': []}),
        event('response.content_part.added', output_index=0, item_id=item['id'], content_index=0,
              part={'type': 'output_text', 'text': '', 'annotations': []}),
        event('response.output_text.delta', output_index=0, item_id=item['id'], content_index=0, delta=text),
        event('response.output_text.done', output_index=0, item_id=item['id'], content_index=0, text=text),
        event('response.content_part.done', output_index=0, item_id=item['id'], content_index=0,
              part=item['content'][0]),
        event('response.output_item.done', output_index=0, item=item),
        completed([item], usage={'input_tokens': 3, 'output_tokens': 2, 'total_tokens': 5}),
    ]


@pytest.mark.parametrize('runtime_type', ['codex', 'omp', 'pi'])
def test_installed_worker_records_requests(monkeypatch, tmp_path, runtime_type):
    from looper.agents.llmrec import control
    from looper.agents.llmrec.proxy import Proxy
    from looper.agents.llmrec.writer import DbSink
    from looper.db import llm_requests

    executable = shutil.which(runtime_type)
    if executable is None:
        pytest.skip(f'{runtime_type} is not on PATH; installed request recording was not exercised')
    # Probes and subprocesses must never fall back to an operator's configuration or login.
    home = tmp_path / 'operator-home'
    home.mkdir()
    monkeypatch.setenv('HOME', str(home))
    for name in ('CODEX_HOME', 'CLAUDE_CONFIG_DIR', 'GROK_HOME', 'PI_CODING_AGENT_DIR', 'OMP_HOME'):
        monkeypatch.delenv(name, raising=False)

    async def body(pool):
        async with FakeModelAPI([ResponseScript(events=_answer())], api_key=KEY) as upstream:
            sink = DbSink(pool)
            proxy = Proxy(tmp_path / 'runtime', sink=sink)
            port = await proxy.start()
            writer_task = asyncio.create_task(sink.run())

            class LocalControl(control.ProxyControl):
                def _state(self):
                    return {'port': port, 'pid': os.getpid(), 'instance': proxy.instance}

                async def _health(self, *, cached=False):
                    return proxy.stats

                async def _start(self, *, port=None):
                    return True

            control.install(LocalControl(tmp_path / 'runtime', '', enabled=True))
            original_send = proxy._client.send

            async def loopback_only(request, **kwargs):
                assert request.url.host == '127.0.0.1', 'installed recording test forbids real providers'
                return await original_send(request, **kwargs)

            monkeypatch.setattr(proxy._client, 'send', loopback_only)
            try:
                runtime_id = await _runtime(runtime_type, executable=executable)
                provider = await _provider('synthetic', base_url=upstream.base_url, protocol='openai-responses')
                profile = (await _profile(provider, 'gpt-5.5', protocol='openai-responses',
                                          runtime_id=runtime_id))['profile']['profile_id']
                await _ok('PUT', '/api/agent-settings/roles/worker', {'profile_id': profile})
                task_id = await _task(tmp_path)
                outcome = await asyncio.wait_for(_work(executable, task_id, tmp_path), timeout=40)
                assert outcome['status'] == 'completed', outcome
                await proxy.close()
                await sink.close(timeout=5)
                record = await pool.fetchrow('SELECT id, metadata FROM llm_records WHERE task_id=$1', task_id)
                assert record is not None
                assert json.loads(record['metadata'])['llmrec']['state'] == 'recording'
                requests = await llm_requests.list_requests(record_id=record['id'], pool=pool)
                assert len(requests) >= 1
                captured = await llm_requests.get_request(requests[0]['id'], pool=pool)
                assert captured['request_body'] == upstream.requests[0].body
                assert upstream.requests[0].path == '/v1/responses'
            finally:
                control.install(None)
                await proxy.close()
                await sink.close(timeout=1)
                writer_task.cancel()
                await asyncio.gather(writer_task, return_exceptions=True)

    run_db(body, monkeypatch, timeout=55)
