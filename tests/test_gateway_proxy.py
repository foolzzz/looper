"""Exercise translated routes over HTTP with a disposable local Chat upstream."""
import asyncio
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
import json

import httpx
import pytest

from looper.agents.llmrec import ROUTES_DIR, RouteFile
from looper.agents.llmrec.proxy import Proxy
from tests.db_support import DSN, run_db


class Sink:
    def __init__(self):
        self.items = []

    def offer(self, capture):
        self.items.append(capture)
        return True


@asynccontextmanager
async def gateway(tmp_path, *, stream=False, record=True, status=200, headers=None, interrupt=False, usage=None):
    received, handlers = [], set()
    usage = usage if usage is not None else {'prompt_tokens': 100, 'completion_tokens': 7,
             'prompt_tokens_details': {'cached_tokens': 40, 'cache_write_tokens': 10}}
    async def upstream(reader, writer):
        task = asyncio.current_task()
        handlers.add(task)
        try:
            head = await reader.readuntil(b'\r\n\r\n')
            fields = dict(line.split(b':', 1) for line in head.split(b'\r\n')[1:] if b':' in line)
            raw = await reader.readexactly(int(fields[b'Content-Length']))
            received.append((head, json.loads(raw)))
            streaming = stream and received[-1][1].get('stream')
            if streaming:
                chunks = [{'id': 'chat-1', 'choices': [{'delta': {'content': 'hello'}, 'finish_reason': 'stop'}]},
                          {'choices': [], 'usage': usage}]
                data = b''.join(b'data: ' + json.dumps(chunk).encode() + b'\n\n' for chunk in chunks)
                if not interrupt:
                    data += b'data: [DONE]\n\n'
            else:
                data = json.dumps({'id': 'chat-1', 'choices': [{'message': {'content': 'hello'},
                      'finish_reason': 'stop'}], 'usage': usage}).encode()
            if status != 200:
                data = b'{"error":{"message":"limited"}}'
            extra = headers or {}
            kind = 'text/event-stream' if streaming else 'application/json'
            writer.write(f'HTTP/1.1 {status} Response\r\nContent-Type: {kind}\r\nConnection: close\r\n'.encode())
            for key, value in extra.items():
                writer.write(f'{key}: {value}\r\n'.encode())
            writer.write(f'Content-Length: {len(data) + (20 if interrupt else 0)}\r\n\r\n'.encode() + data)
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()
            handlers.discard(task)
    server = await asyncio.start_server(upstream, '127.0.0.1', 0)
    sink = Sink()
    route = RouteFile(id='g' * 22, kind='translate', record=record,
                      local_token='local-route-token', provider_id='test-provider',
                      upstream=f'http://127.0.0.1:{server.sockets[0].getsockname()[1]}/v1',
                      api='openai-chat', dialect='openai', models={'client': 'upstream'},
                      created_at='2026-01-01T00:00:00+00:00', agent_id='test')
    route.write(tmp_path / ROUTES_DIR / f'{route.id}.json')
    class Registry:
        target = dict(base_url=route.upstream, api_key='upstream-secret', enabled=True, auth='api_key')

        async def fetchrow(self, query, provider_id):
            assert provider_id == route.provider_id
            return self.target

    sink.pool = Registry()
    proxy = Proxy(tmp_path, sink=sink, read_idle_s=2)
    await proxy.start()
    try:
        async with httpx.AsyncClient(trust_env=False, timeout=5,
                                     headers={'authorization': 'Bearer local-route-token'}) as client:
            yield client, f'http://127.0.0.1:{proxy.port}/r/{route.id}', proxy, sink, received
    finally:
        await proxy.close(grace_s=0)
        server.close()
        await server.wait_closed()
        await asyncio.gather(*handlers, return_exceptions=True)


def run(check):
    asyncio.run(asyncio.wait_for(check(), 10))


@pytest.mark.parametrize('stream', [False, True])
@pytest.mark.parametrize('record', [False, True])
@pytest.mark.parametrize('cached', [(None, None), (None, 0), (40, None)])
def test_translated_run_cache_counts_follow_proxy_usage(tmp_path, monkeypatch, agent_db, stream, record, cached):
    from looper.agents.claude_cli import ClaudeRunRecorder
    from looper.agents.llmrec import LlmrecRoute, RouteKind
    from looper.agents.llmrec import control
    from tests.runtime_model_helpers import builtin_launch

    async def check():
        upstream_usage = {'prompt_tokens': 100, 'completion_tokens': 7}
        async with gateway(tmp_path, stream=stream, record=record, usage=upstream_usage) as (
                client, base, proxy, sink, received):
            owner = control.ProxyControl(tmp_path, '', enabled=record)
            monkeypatch.setattr(owner, '_state', lambda: {'port': proxy.port, 'instance': proxy.instance})
            monkeypatch.setattr(control, '_control', owner)
            route = LlmrecRoute('g' * 22, base, 'http://upstream.invalid/v1', RouteKind.TRANSLATE, record)
            launch = replace(builtin_launch(), llmrec=route)
            # Reuse the route for a second run with no cache reporting, even after a cache-aware run.
            for record_id, counts in enumerate((cached, (None,)), 1):
                agent_db.start_record.return_value = record_id
                recorder = ClaudeRunRecorder(agent_id='test', task_id=None, command=['claude'],
                                            cwd='.', input_text='hi', launch=launch)
                await recorder.start()
                for count in counts:
                    upstream_usage.pop('prompt_tokens_details', None)
                    if count is not None:
                        upstream_usage['prompt_tokens_details'] = {
                            'cached_tokens': count, 'cache_write_tokens': 10 if count else 0}
                    response = await client.post(base + '/v1/messages', json={
                        'model': 'client', 'messages': [], 'stream': stream})
                    assert response.status_code == 200
                    # Assistant snapshots omit cache fields regardless of upstream reporting.
                    await recorder.record({'type': 'assistant', 'message': {
                        'id': f'msg-{len(received)}', 'usage': {'input_tokens': 100, 'output_tokens': 7}}})
                read = sum(count or 0 for count in counts)
                written = sum(10 for count in counts if count)
                tokens = {'inputTokens': 100 * len(counts) - read - written, 'outputTokens': 7 * len(counts),
                          'cacheReadInputTokens': read, 'cacheCreationInputTokens': written}
                result = {'type': 'result', 'subtype': 'success', 'total_cost_usd': 0.01,
                          'modelUsage': {'client': tokens}, 'usage': {
                              'input_tokens': tokens['inputTokens'], 'output_tokens': tokens['outputTokens'],
                              'cache_read_input_tokens': read, 'cache_creation_input_tokens': written}}
                await recorder.finish(output_text='ok', result_event=result)
                stored = agent_db.finish_record.await_args.kwargs
                reported = any(count is not None for count in counts)
                assert (stored['cache_read_tokens'], stored['cache_creation_tokens']) == (
                    read if reported else None, written if reported else None)
                assert (stored['input_tokens'], stored['output_tokens'], stored['cost_usd']) == (
                    tokens['inputTokens'], tokens['outputTokens'], 0.01)
            assert bool(sink.items) is record
    run(check)


@pytest.mark.parametrize('stream', [False, True])
def test_upstream_query_follows_chat_completions_path(tmp_path, stream):
    async def check():
        async with gateway(tmp_path, stream=stream) as (client, base, proxy, sink, received):
            route_path = tmp_path / ROUTES_DIR / ('g' * 22 + '.json')
            route = RouteFile.read(route_path)
            replace(route, upstream=route.upstream + '/?api-version=2025-01-01').write(route_path)
            sink.pool.target['base_url'] += '/?api-version=2025-01-01'
            response = await client.post(base + '/v1/messages', json={
                'model': 'client', 'messages': [], 'stream': stream})
            assert response.status_code == 200
            assert received[0][0].startswith(
                b'POST /v1/chat/completions?api-version=2025-01-01 HTTP/1.1\r\n')
    run(check)


@pytest.mark.skipif(not DSN, reason='requires disposable LOOPER_TEST_DATABASE_URL')
@pytest.mark.parametrize('stream', [False, True])
@pytest.mark.parametrize('reported', [False, True])
def test_translated_cache_counts_preserve_missing_and_reported_zero(tmp_path, stream, reported):
    from looper.db import llm_requests

    async def check(pool):
        usage = {'prompt_tokens': 100, 'completion_tokens': 7}
        if reported:
            usage['prompt_tokens_details'] = {'cached_tokens': 0}
        async with gateway(tmp_path, stream=stream, usage=usage) as (client, base, proxy, sink, received):
            response = await client.post(base + '/v1/messages', json={
                'model': 'client', 'messages': [], 'stream': stream})
            assert response.status_code == 200
            for field in ('cache_read_input_tokens', 'cache_creation_input_tokens'):
                assert (field in response.text) is reported
            async with asyncio.timeout(2):
                while not sink.items:
                    await asyncio.sleep(.01)
            assert len(sink.items) == 1
            await llm_requests.write_batch(sink.items, pool=pool)
            row = await pool.fetchrow('SELECT input_tokens, output_tokens, cache_read_tokens, cache_write_tokens FROM llm_requests')
            assert tuple(row) == (100, 7, 0 if reported else None, 0 if reported else None)
            assert proxy.stats['translate']['cache_fields_missing'] == int(not reported)
    run_db(check, timeout=10)


@pytest.mark.parametrize('stream', [False, True])
@pytest.mark.parametrize('record', [False, True])
def test_translated_exchange_and_client_side_capture(tmp_path, stream, record):
    async def check():
        async with gateway(tmp_path, stream=stream, record=record) as (client, base, proxy, sink, received):
            result = await client.post(base + '/v1/messages', json={'model': 'client', 'messages': [
                {'role': 'user', 'content': 'hi'}], 'max_tokens': 100, 'stream': stream}, headers={
                'authorization': 'Bearer local-route-token', 'x-api-key': 'do-not-forward',
                'anthropic-version': '2023-06-01', 'x-stainless-lang': 'python'})
            assert result.status_code == 200
            if stream:
                assert 'event: message_start' in result.text
                assert 'event: ping' in result.text
                assert 'event: message_stop' in result.text
                assert '"input_tokens":50' in result.text.replace(' ', '')
            else:
                assert result.json()['model'] == 'client'
                assert result.json()['usage']['input_tokens'] == 50
            head, body = received[0]
            assert head.startswith(b'POST /v1/chat/completions HTTP/1.1')
            assert b'authorization: Bearer upstream-secret' in head
            assert b'local-route-token' not in head
            assert b'x-api-key' not in head and b'anthropic-' not in head and b'x-stainless' not in head
            assert body['model'] == 'upstream'
            async with asyncio.timeout(2):
                while record and not sink.items:
                    await asyncio.sleep(.01)
            assert len(sink.items) == int(record)
            if record:
                capture = sink.items[0]
                assert capture.path == '/v1/messages'
                assert capture.translation['upstream_model'] == 'upstream'
                assert capture.translation['upstream_usage']['prompt_tokens'] == 100
                assert capture.translation['upstream_status'] == 200
                assert capture.translation['upstream_request_sha256']
                assert capture.translation['common_prefix_bytes'] is None
                assert (capture.input_tokens, capture.cache_read_tokens, capture.cache_write_tokens,
                        capture.output_tokens) == (50, 40, 10, 7)
            stats = (await client.get(base.split('/r/')[0] + '/llmrec/health')).json()
            assert stats['features'] == {'translate': 1}
            assert stats['translate']['requests'] == 1
            assert stats['translate']['input_tokens'] == 50
    run(check)


@pytest.mark.parametrize('method,path,status,recorded', [('HEAD', '/api/hello', 200, 0),
    ('GET', '/api/hello', 200, 0), ('GET', '/v1/models', 200, 0),
    ('POST', '/v1/messages/count_tokens', 404, 1), ('POST', '/other', 404, 0)])
def test_local_endpoints_never_contact_upstream(tmp_path, method, path, status, recorded):
    async def check():
        async with gateway(tmp_path) as (client, base, proxy, sink, received):
            response = await client.request(method, base + path, json={'model': 'client'})
            assert response.status_code == status
            async with asyncio.timeout(2):
                while recorded and not sink.items:
                    await asyncio.sleep(.01)
            assert not received
            assert len(sink.items) == recorded
            if recorded:
                assert sink.items[0].kind == 'count_tokens'
            if path == '/v1/models':
                assert response.json()['data'][0]['id'] == 'client'
    run(check)


def test_upstream_rate_limit_date_retry_header(tmp_path):
    async def check():
        retry = format_datetime(datetime.now(timezone.utc) + timedelta(seconds=30), usegmt=True)
        async with gateway(tmp_path, status=429, headers={'retry-after': retry, 'x-should-retry': 'true'}) as (client, base, proxy, sink, received):
            response = await client.post(base + '/v1/messages', json={'model': 'client', 'messages': []})
            assert response.status_code == 429
            assert response.json()['error']['type'] == 'rate_limit_error'
            assert 0 <= int(response.headers['retry-after']) <= 30
            assert response.headers['x-should-retry'] == 'true'
    run(check)


def test_interrupted_stream_emits_error_without_message_stop(tmp_path):
    async def check():
        async with gateway(tmp_path, stream=True, interrupt=True) as (client, base, proxy, sink, received):
            result = await client.post(base + '/v1/messages', json={'model': 'client', 'messages': [], 'stream': True})
            assert result.status_code == 200
            assert 'event: error' in result.text
            assert 'event: message_stop' not in result.text
            assert proxy.stats['translate']['stream_errors'] == 1
    run(check)


@pytest.mark.parametrize('body,status', [({'model': 'unknown', 'messages': []}, 404), ([], 400), (None, 400)])
def test_rejection_does_not_contact_upstream(tmp_path, body, status):
    async def check():
        async with gateway(tmp_path) as (client, base, proxy, sink, received):
            result = await client.post(base + '/v1/messages', content=json.dumps(body))
            assert result.status_code == status
            assert result.json()['type'] == 'error'
            assert not received
    run(check)


def test_prefix_lru_eviction_only_loses_observation():
    from looper.agents.llmrec.proxy import _PrefixHistory
    body = b'{"model":"a","messages":[]}'
    history = _PrefixHistory(limit=2 * len(body))
    assert history.remember('a', body) == (None, None, None)
    assert history.remember('b', body) == (None, None, None)
    assert history.remember('a', body) == (len(body), 0, 0)
    assert history.remember('c', body) == (None, None, None)
    assert history.size <= history.limit
    assert history.remember('b', body) == (None, None, None)
    assert history.remember('large', body * 3) == (None, None, None)
    assert history.size <= history.limit
    assert history.remember('large', body * 3) == (None, None, None)


@pytest.mark.parametrize('change', ['append', 'history', 'tools', 'system', 'model', 'session'])
def test_successive_requests_measure_prompt_prefix_reuse(tmp_path, change):
    async def check():
        async with gateway(tmp_path) as (client, base, proxy, sink, received):
            body = {'model': 'client', 'system': 'Stable instructions', 'messages': [
                {'role': 'user', 'content': 'Earlier question ' * 1000}], 'tools': [
                {'name': 'lookup', 'description': 'Tool documentation ' * 4000,
                 'input_schema': {'type': 'object'}}]}
            for turn in range(2):
                if turn:
                    if change == 'history':
                        body['messages'][0]['content'] = 'Rewritten history'
                    elif change == 'tools':
                        body['tools'][0]['description'] = 'Changed tool'
                    elif change == 'system':
                        body['system'] = 'Changed instructions'
                    elif change == 'model':
                        route = proxy._route('g' * 22)
                        replace(route, models={'client': 'another-model'}).write(tmp_path / ROUTES_DIR / f'{route.id}.json')
                    body['messages'].extend([{'role': 'assistant', 'content': 'Answer'},
                                             {'role': 'user', 'content': 'Next question'}])
                response = await client.post(base + '/v1/messages', json=body, headers={
                    'x-claude-code-session-id': 'other' if turn and change == 'session' else 'same'})
                assert response.status_code == 200
            async with asyncio.timeout(2):
                while len(sink.items) < 2:
                    await asyncio.sleep(.01)
            first, second = [item.translation for item in sink.items]
            assert first['common_prefix_tokens'] is None
            assert first['previous_prompt_tokens'] is None
            if change == 'session':
                assert second['common_prefix_tokens'] is None
                assert second['previous_prompt_tokens'] is None
                return
            reuse = second['common_prefix_tokens'] / second['previous_prompt_tokens']
            if change == 'append':
                assert reuse >= .9
                # Tools follow messages in the wire JSON and must still count.
                assert second['common_prefix_bytes'] / second['upstream_request_bytes'] < .4
            elif change == 'history':
                assert .5 < reuse < 1  # unchanged system + tools, rewritten messages
            elif change == 'tools':
                assert 0 < reuse < .1  # only the unchanged system precedes the break
            else:
                assert reuse == 0
    run(check)


@pytest.mark.parametrize('record', [False, True])
@pytest.mark.parametrize('first_stream', [False, True])
def test_stream_start_estimates_context_from_route_prompt_usage(tmp_path, record, first_stream):
    async def check():
        async with gateway(tmp_path, stream=True, record=record) as (client, base, proxy, sink, received):
            messages = [{'role': 'user', 'content': '你好'}]
            previous_bytes = None
            for follow_up in (False, True, True):
                result = await client.post(base + '/v1/messages', json={
                    'model': 'client', 'messages': messages, 'max_tokens': 200 if follow_up else 100,
                    'stream': first_stream or follow_up})
                assert result.status_code == 200
                upstream = received[-1][1]
                body_bytes = len(json.dumps(upstream, ensure_ascii=False, separators=(',', ':')).encode())
                message_bytes = len(json.dumps(upstream['messages'], ensure_ascii=False, separators=(',', ':')).encode())
                expected = 100 + (message_bytes - previous_bytes + 3) // 4 if follow_up else (body_bytes + 3) // 4
                if first_stream or follow_up:
                    frames = [json.loads(line[6:]) for line in result.text.splitlines() if line.startswith('data: ')]
                    assert frames[0]['message']['usage'] == {
                        'input_tokens': expected, 'output_tokens': 0}
                    assert expected > 0
                    usage = frames[-2]['usage']
                else:
                    usage = result.json()['usage']
                assert usage == {
                    'input_tokens': 50, 'cache_read_input_tokens': 40,
                    'cache_creation_input_tokens': 10, 'output_tokens': 7}
                if record:
                    async with asyncio.timeout(2):
                        while len(sink.items) < len(received):
                            await asyncio.sleep(.01)
                    if first_stream or follow_up:
                        assert sink.items[-1].translation['start_usage_estimated'] is True
                        assert sink.items[-1].translation['start_input_tokens'] == expected
                previous_bytes = message_bytes
                if not follow_up:
                    messages += [{'role': 'assistant', 'content': 'hello'},
                                 {'role': 'user', 'content': '继续' * 11}]

            # Compaction or editing history must not reuse the larger prompt.
            messages = [{'role': 'user', 'content': 'short replacement'}]
            result = await client.post(base + '/v1/messages', json={
                'model': 'client', 'messages': messages, 'max_tokens': 100, 'stream': True})
            start = json.loads(result.text.split('data: ', 1)[1].split('\n', 1)[0])
            body_bytes = len(json.dumps(received[-1][1], ensure_ascii=False, separators=(',', ':')).encode())
            assert start['message']['usage']['input_tokens'] == (body_bytes + 3) // 4

            # Another route starts cold even if it serves the same upstream.
            route = RouteFile.read(tmp_path / ROUTES_DIR / ('g' * 22 + '.json'))
            other = replace(route, id='h' * 22)
            other.write(tmp_path / ROUTES_DIR / f'{other.id}.json')
            result = await client.post(base.replace(route.id, other.id) + '/v1/messages', json={
                'model': 'client', 'messages': messages, 'max_tokens': 100, 'stream': True})
            start = json.loads(result.text.split('data: ', 1)[1].split('\n', 1)[0])
            assert start['message']['usage']['input_tokens'] == (body_bytes + 3) // 4
    run(check)


def test_unknown_route_kind_is_rejected_before_upstream(tmp_path):
    async def check():
        async with gateway(tmp_path) as (client, base, proxy, sink, received):
            route_path = tmp_path / ROUTES_DIR / ('g' * 22 + '.json')
            raw = json.loads(route_path.read_text())
            raw['kind'] = 'future'
            route_path.write_text(json.dumps(raw))
            response = await client.post(base + '/v1/messages', json={'model': 'client', 'messages': []})
            assert response.status_code == 404
            assert not received and not sink.items
    run(check)


@pytest.mark.parametrize('failure,attempts', [('connect', 2), ('write', 1), ('read', 1)])
def test_retry_only_connect_establishment_failures(tmp_path, monkeypatch, failure, attempts):
    async def check():
        async with gateway(tmp_path) as (client, base, proxy, sink, received):
            calls = []
            async def failed(*args, **kwargs):
                calls.append(None)
                error = {'connect': httpx.ConnectError, 'write': httpx.WriteError, 'read': httpx.ReadError}[failure]
                raise error('synthetic failure')
            monkeypatch.setattr(proxy._client, 'send', failed)
            response = await client.post(base + '/v1/messages', json={'model': 'client', 'messages': []})
            assert response.status_code == 502
            assert response.json()['error']['type'] == 'api_error'
            assert len(calls) == attempts
    run(check)


def test_idle_pings_keep_one_pending_upstream_read(tmp_path, monkeypatch):
    from looper.agents.llmrec import proxy as module
    monkeypatch.setattr(module, 'PING_INTERVAL_S', .015)
    cancelled = []
    class SlowStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            try:
                # Frequent upstream comments must not suppress client idle pings.
                for _ in range(8):
                    await asyncio.sleep(.006)
                    yield b': keepalive\n\n'
                yield b'data: {"choices":[{"delta":{"content":"done"},"finish_reason":"stop"}]}\n\n'
                yield b'data: [DONE]\n\n'
            except asyncio.CancelledError:
                cancelled.append(True)
                raise
    async def check():
        async with gateway(tmp_path) as (client, base, proxy, sink, received):
            async def respond(*args, **kwargs):
                return httpx.Response(200, stream=SlowStream())
            monkeypatch.setattr(proxy._client, 'send', respond)
            response = await client.post(base + '/v1/messages', json={'model': 'client', 'messages': [], 'stream': True})
            assert response.text.count('event: ping') >= 3
            assert 'event: message_stop' in response.text
            assert not cancelled
    run(check)


@pytest.mark.parametrize('upstream_body', [None, [], {'choices': [None]}, {'choices': []}])
def test_malformed_success_response_returns_anthropic_error(tmp_path, monkeypatch, upstream_body):
    async def check():
        async with gateway(tmp_path) as (client, base, proxy, sink, received):
            async def respond(*args, **kwargs):
                return httpx.Response(200, json=upstream_body)
            monkeypatch.setattr(proxy._client, 'send', respond)
            response = await client.post(base + '/v1/messages', json={'model': 'client', 'messages': []})
            assert response.status_code == 502
            assert response.json()['error']['type'] == 'api_error'
    run(check)


def test_capture_counts_bytes_beyond_retained_buffer(tmp_path, monkeypatch):
    from looper.agents.llmrec import proxy as module
    original = module._ResponseBuffer
    class SmallBuffer(original):
        def append(self, chunk):
            super().append(chunk[:5])
            self.too_large = True
    monkeypatch.setattr(module, '_ResponseBuffer', SmallBuffer)
    async def check():
        async with gateway(tmp_path, stream=True) as (client, base, proxy, sink, received):
            response = await client.post(base + '/v1/messages', json={'model': 'client', 'messages': [], 'stream': True})
            async with asyncio.timeout(2):
                while not sink.items:
                    await asyncio.sleep(.01)
            assert sink.items[0].response_bytes == len(response.content)
            assert sink.items[0].response_bytes > 5
            assert sink.items[0].record_gap == 'response_too_large'
    run(check)


def test_client_disconnect_closes_pending_upstream_stream(tmp_path, monkeypatch):
    async def check():
        closed = asyncio.Event()
        class WaitingStream(httpx.AsyncByteStream):
            async def __aiter__(self):
                yield b'data: {"choices":[{"delta":{"content":"hello"}}]}\n\n'
                await asyncio.Event().wait()
            async def aclose(self):
                closed.set()
        async with gateway(tmp_path) as (client, base, proxy, sink, received):
            async def respond(*args, **kwargs):
                return httpx.Response(200, stream=WaitingStream())
            monkeypatch.setattr(proxy._client, 'send', respond)
            async with client.stream('POST', base + '/v1/messages', json={
                    'model': 'client', 'messages': [], 'stream': True}) as response:
                assert response.status_code == 200
                async for chunk in response.aiter_bytes():
                    if chunk:
                        break
            async with asyncio.timeout(2):
                await closed.wait()
            async with asyncio.timeout(2):
                while proxy._handlers:
                    await asyncio.sleep(.01)
    run(check)


def test_retry_connect_failure_can_succeed_once(tmp_path, monkeypatch):
    async def check():
        async with gateway(tmp_path) as (client, base, proxy, sink, received):
            send = proxy._client.send
            calls = 0
            async def respond(*args, **kwargs):
                nonlocal calls
                calls += 1
                if calls == 1:
                    raise httpx.ConnectError('synthetic connection failure')
                return await send(*args, **kwargs)
            monkeypatch.setattr(proxy._client, 'send', respond)
            response = await client.post(base + '/v1/messages', json={'model': 'client', 'messages': []})
            assert response.status_code == 200
            assert calls == 2 and len(received) == 1
    run(check)


def test_malformed_stream_chunk_returns_anthropic_error_event(tmp_path, monkeypatch):
    class InvalidStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b'data: {"choices":[null]}\n\n'
    async def check():
        async with gateway(tmp_path) as (client, base, proxy, sink, received):
            async def respond(*args, **kwargs):
                return httpx.Response(200, stream=InvalidStream())
            monkeypatch.setattr(proxy._client, 'send', respond)
            response = await client.post(base + '/v1/messages', json={
                'model': 'client', 'messages': [], 'stream': True})
            assert response.status_code == 200
            assert 'event: error' in response.text
            assert 'event: message_stop' not in response.text
            assert proxy.stats['translate']['stream_errors'] == 1
    run(check)


@pytest.mark.parametrize('usage,missing,cache_missing,input_tokens', [
    (None, 1, 0, 0), ({'prompt_tokens': 10}, 0, 1, 10),
    ({'prompt_tokens': 10, 'prompt_tokens_details': {'cached_tokens': 0}}, 0, 0, 10),
    ({'prompt_tokens': 10, 'prompt_tokens_details': {'cache_write_tokens': 3}}, 0, 0, 7),
])
def test_usage_diagnostics_are_observable_without_recording(tmp_path, monkeypatch, usage, missing, cache_missing, input_tokens):
    async def check():
        async with gateway(tmp_path, record=False) as (client, base, proxy, sink, received):
            async def respond(*args, **kwargs):
                return httpx.Response(200, json={'id': 'chat', 'choices': [{
                    'message': {'content': 'hello'}, 'finish_reason': 'stop'}], 'usage': usage})
            monkeypatch.setattr(proxy._client, 'send', respond)
            response = await client.post(base + '/v1/messages', json={'model': 'client', 'messages': []})
            assert response.status_code == 200
            stats = proxy.stats['translate']
            assert stats['usage_missing'] == missing
            assert stats['cache_fields_missing'] == cache_missing
            assert stats['input_tokens'] == input_tokens
            assert not sink.items
    run(check)


@pytest.mark.parametrize('stream', [False, True])
@pytest.mark.parametrize('error_type', [BrokenPipeError, ConnectionResetError])
def test_broken_client_write_is_recorded_without_another_write(tmp_path, monkeypatch, stream, error_type):
    writes_after_disconnect = []
    original = Proxy._translated

    class DisconnectedWriter:
        def __init__(self, writer):
            self.writer = writer
            self.failed = False

        def __getattr__(self, name):
            return getattr(self.writer, name)

        def write(self, data):
            if self.failed:
                writes_after_disconnect.append(data)
            self.writer.write(data)

        async def drain(self):
            self.failed = True
            raise error_type('client disconnected')

    async def broken_client(self, connection, reader, writer, *args):
        return await original(self, connection, reader, DisconnectedWriter(writer), *args)

    monkeypatch.setattr(Proxy, '_translated', broken_client)

    async def check():
        async with gateway(tmp_path, stream=stream) as (client, base, proxy, sink, received):
            try:
                await client.post(base + '/v1/messages', json={'model': 'client', 'messages': [], 'stream': stream})
            except httpx.HTTPError:
                pass
            async with asyncio.timeout(2):
                while not sink.items:
                    await asyncio.sleep(.01)
            assert sink.items[0].outcome == 'client_closed'
            assert not writes_after_disconnect
            assert proxy.stats['translate']['stream_errors'] == 0
    run(check)


@pytest.mark.parametrize('token', [None, 'incorrect', 'upstream-secret'])
def test_route_rejects_invalid_local_token(tmp_path, token):
    async def check():
        async with gateway(tmp_path) as (client, base, proxy, sink, received):
            client.headers.pop('authorization')
            headers = {'authorization': 'Bearer ' + token} if token else {}
            response = await client.post(base + '/v1/messages', headers=headers, json={
                'model': 'client', 'messages': []})
            assert response.status_code == 401
            assert proxy.stats['auth_failures'] == 1
            # Positive control proves the route and upstream are usable with their distinct credentials.
            response = await client.post(base + '/v1/messages', json={'model': 'client', 'messages': []},
                headers={'authorization': 'Bearer local-route-token'})
            assert response.status_code == 200
            assert len(received) == 1
            assert b'authorization: Bearer upstream-secret' in received[0][0]
    run(check)


@pytest.mark.parametrize('failure,status', [('missing', 403), ('disabled', 403), ('endpoint', 403),
                                          ('key', 403), ('database', 503)])
def test_route_credentials_fail_closed(tmp_path, failure, status):
    async def check():
        async with gateway(tmp_path) as (client, base, proxy, sink, received):
            if failure == 'missing':
                sink.pool.target = None
            elif failure == 'database':
                sink.pool = None
            elif failure == 'disabled':
                sink.pool.target['enabled'] = False
            elif failure == 'endpoint':
                sink.pool.target['base_url'] = 'https://changed.invalid/v1'
            else:
                sink.pool.target['api_key'] = ''
            response = await client.post(base + '/v1/messages', json={'model': 'client', 'messages': []})
            assert response.status_code == status
            assert received == []
            assert 'upstream-secret' not in response.text
    run(check)


@pytest.mark.skipif(not DSN, reason='requires disposable LOOPER_TEST_DATABASE_URL')
def test_route_reads_current_upstream_credentials_from_registry(tmp_path):
    async def check(pool):
        async with gateway(tmp_path) as (client, base, proxy, sink, received):
            await pool.execute("""INSERT INTO llm_providers(id,name,vendor_type,auth)
                VALUES ('test-provider','Test account','custom','api_key')""")
            await pool.execute("INSERT INTO llm_provider_secrets VALUES ('test-provider','registry-secret')")
            await pool.execute("""INSERT INTO llm_provider_endpoints(provider_id,protocol,base_url)
                VALUES ('test-provider','openai-chat',$1)""", sink.pool.target['base_url'])
            sink.pool = pool
            for key in ('registry-secret', 'rotated-registry-secret'):
                await pool.execute("UPDATE llm_provider_secrets SET api_key=$1 WHERE provider_id='test-provider'", key)
                response = await client.post(base + '/v1/messages', json={'model': 'client', 'messages': []})
                assert response.status_code == 200
                assert ('authorization: Bearer ' + key).encode() in received[-1][0]
                assert b'local-route-token' not in received[-1][0]
                assert all(key.encode() not in path.read_bytes() for path in tmp_path.rglob('*') if path.is_file())
            await pool.execute("UPDATE llm_providers SET enabled=false WHERE id='test-provider'")
            response = await client.post(base + '/v1/messages', json={'model': 'client', 'messages': []})
            assert response.status_code == 403 and len(received) == 2
    run_db(check, timeout=15)
