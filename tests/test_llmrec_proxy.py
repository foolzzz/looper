import asyncio
from dataclasses import replace
from datetime import datetime, timezone
import gzip
import json
import os

import httpx
import pytest

from looper.agents.llmrec import ROUTES_DIR, RouteFile
from looper.agents.llmrec.proxy import Proxy
from tests.fake_model_api import FakeModelAPI, ResponseScript


class Sink:
    def __init__(self, accept=True):
        self.items = []
        self.accept = accept

    def offer(self, capture):
        self.items.append(capture)
        return self.accept


async def captured(sink, count=1):
    async with asyncio.timeout(3):
        while len(sink.items) < count:
            await asyncio.sleep(0.01)
    return sink.items[-1]


def route(root, upstream, record_id=17):
    value = RouteFile(id='a' * 22, upstream=upstream, agent_id='worker-test',
                      created_at=datetime.now(timezone.utc).isoformat(), record_id=record_id)
    value.write(root / ROUTES_DIR / f'{value.id}.json')
    return value


def events():
    return [{'type': 'message_start', 'message': {'id': 'msg', 'content': [], 'usage': {'input_tokens': 7}}},
            {'type': 'message_delta', 'delta': {'stop_reason': 'end_turn'}, 'usage': {'output_tokens': 3}},
            {'type': 'message_stop'}]


@pytest.mark.parametrize('source', ['provider', 'role'])
@pytest.mark.parametrize('scheme', ['http', 'https'])
def test_recording_uses_launch_proxy_not_server_proxy(tmp_path, monkeypatch, source, scheme):
    from looper.agents.agent_settings import AgentSettings, Launch
    from looper.agents.llmrec.control import ProxyControl
    from looper.agents.profile_registry import builtin_resolved_profile

    # An unreachable server proxy must never override the launch's own proxy.
    monkeypatch.setattr(os, 'environ', {'HTTP_PROXY': 'http://127.0.0.1:1',
                                      'HTTPS_PROXY': 'http://127.0.0.1:1'})

    async def check():
        received = []
        async def forward(reader, writer):
            try:
                head = await reader.readuntil(b'\r\n\r\n')
                received.append(head.split(b'\r\n')[0])
                if scheme == 'http':
                    length = next(int(line.split(b':')[1]) for line in head.split(b'\r\n')
                                  if line.lower().startswith(b'content-length:'))
                    await reader.readexactly(length)
                    writer.write(b'HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\n{}')
                else:
                    # Verify CONNECT reaches this proxy, without requiring TLS or an external provider.
                    writer.write(b'HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\nConnection: close\r\n\r\n')
                await writer.drain()
            finally:
                writer.close()
                await writer.wait_closed()

        server = await asyncio.start_server(forward, '127.0.0.1', 0)
        proxy = Proxy(tmp_path, sink=Sink(), connect_s=0.2)
        await proxy.start()
        try:
            profile = builtin_resolved_profile()
            proxy_env = ((f'{scheme.upper()}_PROXY', f'http://127.0.0.1:{server.sockets[0].getsockname()[1]}'),)
            if source == 'provider':
                resolved = profile.resolved_model
                profile = replace(profile, resolved_model=replace(resolved, provider=replace(resolved.provider, env=proxy_env)))
            launch = Launch(AgentSettings(profile=profile, env=proxy_env if source == 'role' else ()))
            monkeypatch.setattr(type(launch.runtime), 'llmrec_upstream', lambda self, launch: f'{scheme}://127.0.0.1:1')
            control = ProxyControl(tmp_path, '', enabled=True, include_oauth=True)
            async def healthy(**kwargs):
                return proxy.stats
            monkeypatch.setattr(control, '_health', healthy)
            monkeypatch.setattr(control, '_state', lambda: {'port': proxy.port})
            attached = await control.attach(launch, agent_id='worker-test', task_id=None)
            async with httpx.AsyncClient(trust_env=False) as client:
                response = await client.post(attached.llmrec.url + '/v1/messages', json={})
            expected = b'POST http://127.0.0.1:1/v1/messages HTTP/1.1' if scheme == 'http' else b'CONNECT 127.0.0.1:1 HTTP/1.1'
            assert received == [expected]
            assert response.status_code == (200 if scheme == 'http' else 502)
        finally:
            await proxy.close(grace_s=0)
            server.close()
            await server.wait_closed()
    asyncio.run(asyncio.wait_for(check(), 5))


@pytest.mark.parametrize('proxy_env', [
    {},
    {'HTTP_PROXY': 'http://127.0.0.1:1', 'NO_PROXY': '127.0.0.1'},
    {'HTTP_PROXY': 'http://127.0.0.1:1', 'no_proxy': '*'},
    {'HTTP_PROXY': 'http://127.0.0.1:1', 'http_proxy': ''},
])
def test_route_proxy_bypass_and_explicit_empty_environment(tmp_path, monkeypatch, proxy_env):
    monkeypatch.setattr(os, 'environ', {'HTTP_PROXY': 'http://127.0.0.1:1'})
    async def check():
        async with FakeModelAPI([ResponseScript(events=events())]) as upstream:
            value = replace(route(tmp_path, upstream.base_url), proxy_env=proxy_env)
            value.write(tmp_path / ROUTES_DIR / f'{value.id}.json')
            proxy = Proxy(tmp_path, sink=Sink(), connect_s=0.2)
            await proxy.start()
            try:
                async with httpx.AsyncClient(trust_env=False) as client:
                    response = await client.post(f'http://127.0.0.1:{proxy.port}/r/{value.id}/messages',
                                                headers={'authorization': 'Bearer test-key'}, json={})
                assert response.status_code == 200
                assert len(upstream.requests) == 1
            finally:
                await proxy.close(grace_s=0)
    asyncio.run(asyncio.wait_for(check(), 5))


def test_passthrough_headers_base_path_and_binding(tmp_path):
    async def check():
        sink = Sink()
        scripts = [ResponseScript(events=events()), ResponseScript(events=events())]
        async with FakeModelAPI(scripts) as upstream:
            value = route(tmp_path, upstream.base_url)
            proxy = Proxy(tmp_path, sink=sink)
            await proxy.start()
            try:
                async with httpx.AsyncClient() as client:
                    client.headers.clear()
                    url = f'http://127.0.0.1:{proxy.port}/r/{value.id}/messages'
                    response = await client.post(url, headers={'authorization': 'Bearer test-key'},
                                                 json={'model': 'haiku', 'messages': [], 'stream': True})
                    assert response.status_code == 200
                    assert response.content == b''.join(('data: ' + json.dumps(e) + '\n\n').encode() for e in events())
                    capture = await captured(sink)
                    assert capture.record_id == 17
                    assert capture.request_headers['authorization'] == '<redacted>'
                    assert upstream.requests[0].path == '/v1/messages'
                    assert upstream.requests[0].headers['authorization'] == 'Bearer test-key'
                    assert 'user-agent' not in upstream.requests[0].headers
                    assert 'accept-encoding' not in upstream.requests[0].headers
                    replace(value, record_id=18).write(tmp_path / ROUTES_DIR / f'{value.id}.json')
                    assert (await client.post(url, headers={'authorization': 'Bearer test-key'}, json={'messages': []})).status_code == 200
                    assert (await captured(sink, 2)).record_id == 18
            finally:
                await proxy.close(grace_s=0)
    asyncio.run(check())


def test_unknown_route_and_refused_upstream(tmp_path):
    async def check():
        sink = Sink()
        async with FakeModelAPI([]) as upstream:
            route(tmp_path, 'http://127.0.0.1:1')
            proxy = Proxy(tmp_path, sink=sink, connect_s=0.2)
            await proxy.start()
            try:
                async with httpx.AsyncClient() as client:
                    assert (await client.post(f'http://127.0.0.1:{proxy.port}/r/unknown/messages', json={})).status_code == 404
                    assert upstream.requests == []
                    response = await client.post(f'http://127.0.0.1:{proxy.port}/r/{"a" * 22}/messages', json={})
                    assert response.status_code == 502
                    assert (await captured(sink)).outcome == 'upstream_error'
            finally:
                await proxy.close(grace_s=0)
    asyncio.run(check())


def test_stream_gate_and_client_disconnect(tmp_path):
    async def check():
        sink = Sink()
        gate = asyncio.Event()
        # The fake API gates response.completed after forwarding the first Anthropic event.
        script = ResponseScript(events=[events()[0], {'type': 'response.completed', 'response': {}}], gate=gate)
        async with FakeModelAPI([script]) as upstream:
            value = route(tmp_path, upstream.base_url)
            proxy = Proxy(tmp_path, sink=sink)
            await proxy.start()
            try:
                reader, writer = await asyncio.open_connection('127.0.0.1', proxy.port)
                body = b'{"messages":[]}'
                writer.write((f'POST /r/{value.id}/messages HTTP/1.1\r\nHost: localhost\r\nAuthorization: Bearer test-key\r\nContent-Length: {len(body)}\r\n\r\n').encode() + body)
                await writer.drain()
                async with asyncio.timeout(3):
                    assert b'200' in await reader.readuntil(b'\r\n\r\n')
                    await script.gate_reached.wait()
                    assert b'message_start' in await reader.readuntil(b'\n\n')
                replace(value, record_id=99).write(tmp_path / ROUTES_DIR / f'{value.id}.json')
                writer.close()
                await writer.wait_closed()
                capture = await captured(sink)
                assert capture.outcome == 'client_closed'
                assert capture.record_id == 17
                await asyncio.wait_for(script.closed.wait(), 3)
            finally:
                gate.set()
                await proxy.close(grace_s=0)
    asyncio.run(check())


def test_upstream_disconnect_and_sink_refusal(tmp_path):
    async def check():
        sink = Sink(accept=False)
        async with FakeModelAPI([ResponseScript(events=events()), ResponseScript(events=events(), disconnect_after=1)]) as upstream:
            value = route(tmp_path, upstream.base_url)
            proxy = Proxy(tmp_path, sink=sink)
            await proxy.start()
            try:
                async with httpx.AsyncClient() as client:
                    url = f'http://127.0.0.1:{proxy.port}/r/{value.id}/messages'
                    assert (await client.post(url, headers={'authorization': 'Bearer test-key'}, json={})).status_code == 200
                    await captured(sink)
                    assert proxy.stats['dropped'] == 1
                    with pytest.raises(httpx.RemoteProtocolError):
                        await client.post(url, headers={'authorization': 'Bearer test-key'}, json={})
                    assert (await captured(sink, 2)).outcome == 'upstream_error'
            finally:
                await proxy.close(grace_s=0)
    asyncio.run(check())


def test_gzip_raw_bytes_and_assembled_usage(tmp_path):
    async def check():
        sink = Sink()
        payload = b''.join(('data: ' + json.dumps(e) + '\n\n').encode() for e in events())
        compressed = gzip.compress(payload)
        async def upstream(reader, writer):
            try:
                head = await reader.readuntil(b'\r\n\r\n')
                headers = dict(line.split(b':', 1) for line in head.split(b'\r\n')[1:] if b':' in line)
                await reader.readexactly(int(headers[b'Content-Length']))
                writer.write(f'HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nContent-Encoding: gzip\r\nContent-Length: {len(compressed)}\r\n\r\n'.encode())
                for index in range(0, len(compressed), 7):
                    writer.write(compressed[index:index + 7])
                    await writer.drain()
            finally:
                writer.close()
                await writer.wait_closed()
        server = await asyncio.start_server(upstream, '127.0.0.1', 0)
        value = route(tmp_path, f'http://127.0.0.1:{server.sockets[0].getsockname()[1]}')
        proxy = Proxy(tmp_path, sink=sink)
        await proxy.start()
        try:
            async with httpx.AsyncClient() as client:
                async with client.stream('POST', f'http://127.0.0.1:{proxy.port}/r/{value.id}/v1/messages',
                                         headers={'accept-encoding': 'gzip'}, json={'model': 'haiku'}) as response:
                    assert b''.join([chunk async for chunk in response.aiter_raw()]) == compressed
            capture = await captured(sink)
            assert capture.record_gap is None
            assert capture.response_body['id'] == 'msg'
            assert capture.input_tokens == 7
            assert capture.output_tokens == 3
        finally:
            await proxy.close(grace_s=0)
            server.close()
            await server.wait_closed()
    asyncio.run(check())


def test_binding_is_stamped_before_slow_request_body(tmp_path):
    async def check():
        sink = Sink()
        async with FakeModelAPI([ResponseScript(events=events())]) as upstream:
            value = route(tmp_path, upstream.base_url)
            proxy = Proxy(tmp_path, sink=sink)
            await proxy.start()
            writer = None
            try:
                reader, writer = await asyncio.open_connection('127.0.0.1', proxy.port)
                body = b'{"messages":[]}'
                writer.write((f'POST /r/{value.id}/messages HTTP/1.1\r\nHost: localhost\r\nAuthorization: Bearer test-key\r\nContent-Length: {len(body)}\r\nConnection: close\r\n\r\n').encode() + body[:1])
                await writer.drain()
                async with asyncio.timeout(3):
                    while proxy.stats['requests'] != 1:
                        await asyncio.sleep(0.01)
                replace(value, record_id=99).write(tmp_path / ROUTES_DIR / f'{value.id}.json')
                writer.write(body[1:])
                await writer.drain()
                assert b'200' in await asyncio.wait_for(reader.read(), 3)
                assert (await captured(sink)).record_id == 17
            finally:
                if writer:
                    writer.close()
                    await writer.wait_closed()
                await proxy.close(grace_s=0)
    asyncio.run(check())


def test_unknown_response_encoding_is_marked(tmp_path):
    async def check():
        sink = Sink()
        script = ResponseScript(events=events(), headers={'Content-Encoding': 'unknown'})
        async with FakeModelAPI([script]) as upstream:
            value = route(tmp_path, upstream.base_url)
            proxy = Proxy(tmp_path, sink=sink)
            await proxy.start()
            try:
                async with httpx.AsyncClient() as client:
                    async with client.stream('POST', f'http://127.0.0.1:{proxy.port}/r/{value.id}/messages',
                                             headers={'authorization': 'Bearer test-key'}, json={}) as response:
                        raw = b''.join([chunk async for chunk in response.aiter_raw()])
                assert raw.startswith(b'data: ')
                assert (await captured(sink)).record_gap == 'body_encoding_unparsed'
            finally:
                await proxy.close(grace_s=0)
    asyncio.run(check())
