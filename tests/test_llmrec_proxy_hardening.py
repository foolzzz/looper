import asyncio
from contextlib import asynccontextmanager
from dataclasses import replace
import gzip
import json

import httpx
import pytest

from looper.agents.llmrec.capture import build_capture
from looper.agents.llmrec.proxy import Proxy, _ProxyTransport, _upstream_proxy
from tests.test_llmrec_proxy import Sink, captured, events, route


@pytest.mark.parametrize('lower,expected', [
    (None, None),
    ('', 'http://127.0.0.1:1'),
    ('internal.example', None),
])
def test_upstream_lowercase_no_proxy_takes_precedence(tmp_path, lower, expected):
    fake_proxy = 'http://127.0.0.1:1'
    proxy_env = {'NO_PROXY': 'internal.example', 'HTTPS_PROXY': fake_proxy}
    if lower is not None:
        proxy_env['no_proxy'] = lower
    value = replace(route(tmp_path, 'https://internal.example'), proxy_env=proxy_env)
    assert _upstream_proxy(value, 'https://external.example/v1/messages') == fake_proxy
    assert _upstream_proxy(value, 'https://internal.example/v1/messages') == expected


@asynccontextmanager
async def upstream(reply):
    requests, handlers = [], set()

    async def handle(reader, writer):
        task = asyncio.current_task()
        handlers.add(task)
        try:
            head = await reader.readuntil(b'\r\n\r\n')
            headers = dict(line.lower().split(b':', 1) for line in head.split(b'\r\n') if b':' in line)
            await reader.readexactly(int(headers.get(b'content-length', b'0')))
            path = head.split(b' ')[1].decode()
            requests.append(path)
            status, response_headers, body = await reply(path)
            writer.write(f'HTTP/1.1 {status} Test\r\nConnection: close\r\nContent-Length: {len(body)}\r\n'.encode())
            writer.write(b''.join(f'{key}: {value}\r\n'.encode() for key, value in response_headers.items()))
            writer.write(b'\r\n' + body)
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()
            handlers.discard(task)

    server = await asyncio.start_server(handle, '127.0.0.1', 0)
    try:
        yield f'http://127.0.0.1:{server.sockets[0].getsockname()[1]}', requests
    finally:
        server.close()
        await server.wait_closed()
        pending = list(handlers)
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)


@pytest.mark.parametrize('base_suffix', ['/gateway?provider=one#ignored', '/gateway#ignored'])
def test_upstream_base_path_query_and_fragment_are_separate(tmp_path, base_suffix):
    async def check():
        async def reply(path):
            return 200, {'Content-Type': 'application/json'}, b'{}'

        async with upstream(reply) as (base, requests):
            value = route(tmp_path, base + base_suffix)
            sink = Sink()
            proxy = Proxy(tmp_path, sink=sink)
            await proxy.start()
            try:
                async with httpx.AsyncClient(trust_env=False) as client:
                    response = await client.post(f'http://127.0.0.1:{proxy.port}/r/{value.id}/v1/messages?request=two', json={})
                assert response.status_code == 200
                query = 'provider=one&request=two' if '?' in base_suffix else 'request=two'
                assert requests == [f'/gateway/v1/messages?{query}']
                assert (await captured(sink)).path == requests[0]
            finally:
                await proxy.close(grace_s=0)
    asyncio.run(asyncio.wait_for(check(), 5))


@pytest.mark.parametrize('location', ['next', '../next', '/gateway/next', 'same-origin',
                                     '/outside', 'same-origin-outside', '?redirect=1', 'external'])
def test_redirect_location_keeps_recording_route(tmp_path, location):
    async def check():
        base_query = '?provider=one' if location in ('same-origin', '?redirect=1') else ''

        async def reply(path):
            if path == '/gateway/v1/messages' + base_query:
                target = base + '/gateway/next?provider=one&redirect=1#anchor' if location == 'same-origin' else location
                if location == 'same-origin-outside':
                    target = base + '/outside?redirect=1'
                if location == 'external':
                    target = 'https://other.example/next'
                return 307, {'Location': target}, b'{}'
            return 200, {'Content-Type': 'application/json'}, b'{}'

        async with upstream(reply) as (base, requests):
            value = route(tmp_path, base + '/gateway' + base_query)
            sink = Sink()
            proxy = Proxy(tmp_path, sink=sink)
            await proxy.start()
            try:
                async with httpx.AsyncClient(trust_env=False) as client:
                    response = await client.post(f'http://127.0.0.1:{proxy.port}/r/{value.id}/v1/messages', json={})
                    if location == 'external':
                        assert response.headers['location'] == 'https://other.example/next'
                        return
                    redirected = await client.send(response.next_request)
                assert redirected.status_code == 200
                target = {'next': '/gateway/v1/next', '../next': '/gateway/next',
                          '/gateway/next': '/gateway/next', 'same-origin': '/gateway/next?provider=one&redirect=1',
                          '/outside': '/outside', 'same-origin-outside': '/outside?redirect=1',
                          '?redirect=1': '/gateway/v1/messages?redirect=1'}[location]
                assert requests == ['/gateway/v1/messages' + base_query, target]
                assert f'/r-origin/{value.id}/' in response.headers['location']
                await captured(sink, 2)
                assert all(item.route.id == value.id for item in sink.items)
                assert target in [item.path for item in sink.items]
            finally:
                await proxy.close(grace_s=0)
    asyncio.run(asyncio.wait_for(check(), 5))


def test_default_proxy_timeout_leaves_read_deadline_to_client(tmp_path, monkeypatch):
    async def check():
        read_timeouts = []
        original = _ProxyTransport.handle_async_request

        async def observe(transport, request):
            read_timeouts.append(request.extensions['timeout']['read'])
            return await original(transport, request)

        monkeypatch.setattr(_ProxyTransport, 'handle_async_request', observe)

        async def reply(path):
            await asyncio.sleep(0.03)
            return 200, {'Content-Type': 'application/json'}, b'{}'

        async with upstream(reply) as (base, requests):
            value = route(tmp_path, base)
            proxy = Proxy(tmp_path, sink=Sink())
            await proxy.start()
            try:
                async with httpx.AsyncClient(trust_env=False, timeout=1) as client:
                    response = await client.post(f'http://127.0.0.1:{proxy.port}/r/{value.id}/v1/messages', json={})
                assert response.status_code == 200
                assert requests == ['/v1/messages']
                assert read_timeouts == [None]
            finally:
                await proxy.close(grace_s=0)
    asyncio.run(asyncio.wait_for(check(), 5))


def test_multimember_gzip_keeps_later_usage(tmp_path):
    async def check():
        stream = [f'data: {json.dumps(event)}\n\n'.encode() for event in events()]
        compressed = gzip.compress(stream[0]) + gzip.compress(b''.join(stream[1:]))

        async def reply(path):
            return 200, {'Content-Type': 'text/event-stream', 'Content-Encoding': 'gzip'}, compressed

        async with upstream(reply) as (base, requests):
            value = route(tmp_path, base)
            sink = Sink()
            proxy = Proxy(tmp_path, sink=sink)
            await proxy.start()
            try:
                async with httpx.AsyncClient(trust_env=False) as client:
                    async with client.stream('POST', f'http://127.0.0.1:{proxy.port}/r/{value.id}/v1/messages', json={}) as response:
                        raw = b''.join([chunk async for chunk in response.aiter_raw()])
                assert response.status_code == 200
                assert raw == compressed
                result = await captured(sink)
                assert result.record_gap is None
                assert result.output_tokens == 3
                assert result.response_body['stop_reason'] == 'end_turn'
                assert requests == ['/v1/messages']
            finally:
                await proxy.close(grace_s=0)
    asyncio.run(asyncio.wait_for(check(), 5))


def test_unknown_anthropic_content_delta_marks_stream_gap():
    stream = [events()[0],
              {'type': 'content_block_start', 'index': 0, 'content_block': {'type': 'compaction'}},
              {'type': 'content_block_delta', 'index': 0, 'delta': {'type': 'compaction_delta', 'content': 'summary'}},
              *events()[1:]]
    payload = ''.join(f'data: {json.dumps(event)}\n\n' for event in stream).encode()
    result = build_capture(request_body=b'{}', response_data=payload, path='/v1/messages',
                           response_headers={'content-type': 'text/event-stream'})
    assert result.record_gap == 'unparsable_stream'
    assert result.response_body is None
    assert 'compaction_delta' in result.response_raw
