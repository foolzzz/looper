"""Worker subprocesses record their actual HTTP exchanges against a loopback provider."""

import asyncio
import json
import os
from pathlib import Path

import pytest

from fake_claude import fake_claude
from tests.db_support import DSN, run_db
from tests.fake_model_api import FakeModelAPI, ResponseScript
from tests.test_profile_registry_db import _ok
from tests.test_provider_binding_db import KEY, _profile, _provider, _task, _work

pytestmark = pytest.mark.skipif(not DSN, reason="requires disposable LOOPER_TEST_DATABASE_URL")

_EVENTS = [
    {"type": "message_start", "message": {"id": "m-test", "type": "message", "role": "assistant",
      "model": "haiku", "content": [], "usage": {"input_tokens": 3}}},
    {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
    {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "ok"}},
    {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 2}},
    {"type": "message_stop"},
]


def _standin(path: Path, seen: Path, *, reviewer=False):
    result = {"type": "result", "subtype": "success", "is_error": False,
        "result": json.dumps({'passed': True, 'summary': 'ok', 'findings': []} if reviewer else
                             {'status': 'completed', 'summary': 'ok', 'changed_files': {}}),
        "session_id": "s-record-test", "total_cost_usd": 0,
        "usage": {"input_tokens": 3, "output_tokens": 2}}
    return fake_claude(path, "import os, sys, json, urllib.request\n"
        "if '--version' in sys.argv:\n    print('2.1.285 (Claude Code)'); sys.exit(0)\n"
        "sys.stdin.readline()\n"
        f"seen = {str(seen)!r}\n"
        "env = {key: os.environ.get(key) for key in ('ANTHROPIC_BASE_URL', 'ANTHROPIC_API_KEY', "
        "'ANTHROPIC_AUTH_TOKEN', 'NO_PROXY', 'no_proxy')}\n"
        "with open(seen, 'w') as out: json.dump(env, out)\n"
        + "body = json.dumps({'model': 'haiku', 'system': [{'type': 'text', 'text': 'test system'}], "
           "'tools': [{'name': 'read', 'input_schema': {'type': 'object'}}], "
           "'messages': [{'role': 'user', 'content': 'hello'}], 'stream': True}).encode()\n"
           f"key = os.environ.get('ANTHROPIC_AUTH_TOKEN') or os.environ.get('ANTHROPIC_API_KEY') or {KEY!r}\n"
           "req = urllib.request.Request(os.environ['ANTHROPIC_BASE_URL'].rstrip('/') + '/v1/messages', "
           "body, {'content-type': 'application/json', 'authorization': 'Bearer ' + key})\n"
           "with urllib.request.urlopen(req, timeout=10) as response: response.read()\n"
        + f"print(json.dumps({result!r}), flush=True)\nsys.stdin.read()\n")


@pytest.mark.parametrize("case,recorded", [("third-party", True), ("reviewer", True), ("official", True),
    ("oauth-off", False), ("oauth-on", True), ("off", False), ("unavailable", False)])
def test_worker_recording_binding_and_bypass(monkeypatch, tmp_path, case, recorded):
    from looper.agents.llmrec import LlmrecRoute, ROUTES_DIR, RouteFile
    from looper.agents.llmrec import control
    from looper.agents.llmrec.proxy import Proxy
    from looper.agents.llmrec.writer import DbSink
    from looper.db import llm_requests

    async def body(pool):
        async with FakeModelAPI([ResponseScript(events=_EVENTS)], api_key=KEY) as upstream:
            origin = upstream.base_url.removesuffix('/v1')
            base = origin + '/anthropic'
            monkeypatch.setenv('ANTHROPIC_BASE_URL', base)
            sink = DbSink(pool)
            proxy = Proxy(tmp_path / 'runtime', sink=sink)
            port = await proxy.start()
            writer_task = asyncio.create_task(sink.run())

            class LocalControl(control.ProxyControl):
                def _state(self):
                    return {'port': port, 'pid': os.getpid(), 'instance': proxy.instance}

                async def _health(self, *, cached=False):
                    return None if case == 'unavailable' else proxy.stats

                async def _start(self, *, port=None):
                    return case != 'unavailable'

            owner = LocalControl(tmp_path / 'runtime', '', enabled=case != 'off', include_oauth=case == 'oauth-on')
            control.install(owner)
            launches = []
            original_attach = control.attach

            async def observe(launch, **kwargs):
                attached = await original_attach(launch, **kwargs)
                launches.append((launch, attached))
                assert attached.binding_sha256 == launch.binding_sha256
                assert attached.env_sha256 == launch.env_sha256
                return attached

            monkeypatch.setattr(control, 'attach', observe)
            original_send = proxy._client.send

            async def loopback_only(request, **kwargs):
                # Keep the official binding and route intact while ensuring this test can never contact a provider.
                if request.url.host == 'api.anthropic.com':
                    request.url = request.url.copy_with(scheme='http', host='127.0.0.1',
                        port=int(origin.rsplit(':', 1)[1]))
                assert request.url.host == '127.0.0.1'
                return await original_send(request, **kwargs)

            monkeypatch.setattr(proxy._client, 'send', loopback_only)
            try:
                if case.startswith('oauth'):
                    profile = (await _profile('prov-claude-subscription', 'haiku'))['profile']['profile_id']
                else:
                    provider = await _provider('test-key', base_url='' if case == 'official' else base)
                    profile = (await _profile(provider, 'haiku'))['profile']['profile_id']
                role = 'reviewer' if case == 'reviewer' else 'worker'
                await _ok('PUT', f'/api/agent-settings/roles/{role}', {'profile_id': profile})
                task = await _task(tmp_path)
                seen = tmp_path / 'seen.json'
                executable = _standin(tmp_path / 'claude', seen, reviewer=case == 'reviewer')
                if case == 'reviewer':
                    from looper.agents.message import MessageEnvelope, MessageType, planner_address, worker_address
                    from looper.agents.tiers.worker import WorkerConfig, WorkerPool

                    (tmp_path / 'w').mkdir(parents=True, exist_ok=True)
                    message = MessageEnvelope.create(source=planner_address(task), target=worker_address('reviewer-test'),
                        message_type=MessageType.WORK_ITEM, payload={'task_id': 'step-review',
                        'parent_task_id': task, 'instruction': 'review it',
                        'context': {'work_dir': str(tmp_path / 'w')}})
                    outcome = await WorkerPool(config=WorkerConfig(claude_bin=executable),
                        runtime_root=tmp_path / 'runtime')._execute(message, is_verify=True)
                    assert outcome['passed'], outcome
                else:
                    outcome = await _work(executable, task, tmp_path)
                    assert outcome['status'] == 'completed', outcome
                await proxy.close()
                await sink.close(timeout=5)
                row = await pool.fetchrow('SELECT id, metadata FROM llm_records WHERE task_id=$1', task)
                assert row is not None
                metadata = json.loads(row['metadata'])
                rows = await llm_requests.list_requests(record_id=row['id'], pool=pool)
                assert len(rows) == int(recorded)
                environment = json.loads(seen.read_text())
                before, after = launches[0]
                if recorded:
                    assert isinstance(after.llmrec, LlmrecRoute)
                    assert environment['ANTHROPIC_BASE_URL'].startswith(f'http://127.0.0.1:{port}/r/')
                    assert '127.0.0.1' in environment['NO_PROXY'].split(',')
                    assert '127.0.0.1' in environment['no_proxy'].split(',')
                    route = RouteFile.read(tmp_path / 'runtime' / ROUTES_DIR / f'{after.llmrec.id}.json')
                    assert route.ended_at and route.record_id is None and route.last_record_id == row['id']
                    assert route.role == role
                    assert metadata['llmrec']['state'] == 'recording'
                    saved = await llm_requests.get_request(rows[0]['id'], pool=pool)
                    assert saved['response_body']['content'][0]['text'] == 'ok'
                    assert (saved['input_tokens'], saved['output_tokens']) == (3, 2)
                    assert saved['request_body']['system'] and saved['request_body']['tools']
                    assert saved['request_headers']['authorization'] == '<redacted>'
                    if case == 'official':
                        assert after.llmrec.upstream == 'https://api.anthropic.com'
                        assert environment['ANTHROPIC_API_KEY'] == KEY
                        assert environment['ANTHROPIC_AUTH_TOKEN'] is None
                        assert upstream.requests[0].path == '/v1/messages'
                    else:
                        assert upstream.requests[0].path == '/anthropic/v1/messages'
                elif case == 'unavailable':
                    assert metadata['llmrec']['state'] == 'bypassed'
                    assert metadata['llmrec']['reason'] == 'proxy_unavailable'
                    assert environment['ANTHROPIC_BASE_URL'] == base
                else:
                    assert 'llmrec' not in metadata
                    assert after.environment() == before.environment()
                    assert environment['ANTHROPIC_BASE_URL'] == base
            finally:
                control.install(None)
                await proxy.close()
                await sink.close(timeout=1)
                writer_task.cancel()
                await asyncio.gather(writer_task, return_exceptions=True)
    run_db(body, monkeypatch, timeout=35)


def test_codex_api_key_worker_is_not_covered(monkeypatch, tmp_path):
    from looper.agents.llmrec import control
    from tests.test_profile_registry_db import _runtime

    async def body(pool):
        report = json.dumps({'status': 'completed', 'summary': 'ok', 'changed_files': {}})
        events = [
            {'type': 'thread.started', 'thread_id': 'thread-test'},
            {'type': 'turn.started'},
            {'type': 'item.completed', 'item': {'id': 'i-test', 'type': 'agent_message', 'text': report}},
            {'type': 'turn.completed', 'usage': {'input_tokens': 3, 'output_tokens': 2}},
        ]
        executable = fake_claude(tmp_path / 'codex',
            'import json, sys\n' + f'for item in {events!r}:\n    print(json.dumps(item), flush=True)\n')
        runtime = await _runtime('codex', executable=executable)
        # This stand-in implements Codex's exec protocol, without its app-server steering capability.
        await pool.execute('UPDATE agent_runtimes SET capabilities=$1 WHERE id=$2', ['run'], runtime)
        provider = await _provider('codex-key', base_url='http://127.0.0.1:1/v1', protocol='openai-responses')
        profile = (await _profile(provider, 'gpt-test', protocol='openai-responses',
                                  runtime_id=runtime))['profile']['profile_id']
        await _ok('PUT', '/api/agent-settings/roles/worker', {'profile_id': profile})
        task = await _task(tmp_path)
        owner = control.ProxyControl(tmp_path / 'runtime', '', enabled=True)
        control.install(owner)
        try:
            result = await _work(executable, task, tmp_path)
            assert result['status'] == 'completed', result
            metadata = json.loads(await pool.fetchval('SELECT metadata FROM llm_records WHERE task_id=$1', task))
            assert metadata['llmrec']['state'] == 'skipped'
            assert metadata['llmrec']['reason'] == 'not_covered'
            assert await pool.fetchval('SELECT count(*) FROM llm_requests') == 0
        finally:
            control.install(None)
    run_db(body, monkeypatch, timeout=25)
