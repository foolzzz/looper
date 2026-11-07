"""External Anthropic clients use aliases and a distinct local credential."""
import asyncio
from contextlib import asynccontextmanager
import json
import logging

import httpx
import pytest

from looper.agents.llmrec.gateway import GatewayConfig, GatewayKey
from looper.agents.llmrec.proxy import Proxy
from looper.agents.llmrec.writer import DbSink
from tests.db_support import DSN, run_db, until

pytestmark = pytest.mark.skipif(not DSN, reason='requires disposable LOOPER_TEST_DATABASE_URL')


@asynccontextmanager
async def endpoint(root, pool, *, record=False):
    received = []
    handlers = set()
    async def upstream(reader, writer):
        handlers.add(asyncio.current_task())
        try:
            head = await reader.readuntil(b'\r\n\r\n')
            headers = {k.lower(): v.strip() for k, v in
                       (line.split(b':', 1) for line in head.split(b'\r\n')[1:] if b':' in line)}
            body = json.loads(await reader.readexactly(int(headers[b'content-length'])))
            received.append((headers, body))
            data = json.dumps({'id': 'chat-test', 'choices': [{'message': {'content': 'hello'},
                'finish_reason': 'stop'}], 'usage': {'prompt_tokens': 10, 'completion_tokens': 2,
                'prompt_tokens_details': {'cached_tokens': 4}}}).encode()
            writer.write(b'HTTP/1.1 200 OK\r\nConnection: close\r\nContent-Type: application/json\r\n'
                         + f'Content-Length: {len(data)}\r\n\r\n'.encode() + data)
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()
            handlers.discard(asyncio.current_task())
    server = await asyncio.start_server(upstream, '127.0.0.1', 0)
    await pool.execute("INSERT INTO llm_providers(id,name,vendor_type,auth) VALUES ('prov-external','External','custom','api_key')")
    await pool.execute("INSERT INTO llm_provider_secrets VALUES ('prov-external','upstream-secret')")
    await pool.execute("INSERT INTO llm_provider_endpoints(provider_id,protocol,base_url) VALUES ('prov-external','openai-chat',$1)",
                       f'http://127.0.0.1:{server.sockets[0].getsockname()[1]}/v1')
    await pool.execute("INSERT INTO llm_models(id,provider_id,protocol,name) VALUES ('model-external','prov-external','openai-chat','upstream')")
    await pool.execute("INSERT INTO gateway_aliases(alias,model_id) VALUES ('local-chat','model-external')")
    key = GatewayKey(root).rotate()
    GatewayConfig(root).write(enabled=True, record=record)
    sink = DbSink(pool)
    writer_task = asyncio.create_task(sink.run())
    proxy = Proxy(root, sink=sink)
    await proxy.start()
    try:
        async with httpx.AsyncClient(trust_env=False, timeout=3) as client:
            yield client, f'http://127.0.0.1:{proxy.port}/gw', proxy, received, key, sink
    finally:
        await proxy.close(grace_s=1)
        await sink.close(timeout=3)
        await writer_task
        server.close()
        await server.wait_closed()
        await asyncio.gather(*handlers, return_exceptions=True)


@pytest.mark.parametrize('header,status', [('x-api-key',200),('Authorization',200),('wrong',401),('missing',401)])
def test_external_authentication_and_upstream_credential(tmp_path, header, status):
    async def check(pool):
        async with endpoint(tmp_path, pool) as (client, base, proxy, received, key, sink):
            headers = {'x-api-key': key} if header == 'x-api-key' else (
                {'Authorization': 'Bearer ' + key} if header == 'Authorization' else
                {'x-api-key': 'invalid'} if header == 'wrong' else {})
            response = await client.post(base + '/v1/messages', headers=headers,
                                         json={'model':'local-chat','messages':[],'max_tokens':8})
            assert response.status_code == status
            assert 'access-control-allow-origin' not in response.headers
            if status == 200:
                assert received[0][0][b'authorization'] == b'Bearer upstream-secret'
                assert b'x-api-key' not in received[0][0]
                assert received[0][1]['model'] == 'upstream'
                assert response.json()['model'] == 'local-chat'
                await until(lambda: proxy.stats['alias_usage'].get('local-chat', {}).get('requests') == 1,
                            'alias counters', timeout=2)
                assert not sink.pending and not sink.recorded
            else:
                assert not received
                assert proxy.stats['auth_failures'] == 1
    run_db(check, timeout=15)


@pytest.mark.parametrize('headers', [{'Origin':'http://localhost'}, {'Origin':''}, {'Host':'attacker.invalid'}, {'Host':'localhost:1'}])
def test_external_browser_guard_precedes_authentication(tmp_path, headers):
    async def check(pool):
        async with endpoint(tmp_path, pool) as (client, base, proxy, received, key, sink):
            response = await client.get(base + '/v1/models', headers=headers)
            assert response.status_code == 403
            assert not received
            assert proxy.stats['auth_failures'] == 0
    run_db(check, timeout=15)


@pytest.mark.parametrize('method,path,status', [('GET','/v1/models',200),('HEAD','/api/hello',200),
                                                ('POST','/v1/messages/count_tokens',404)])
def test_external_local_paths(tmp_path, method, path, status):
    async def check(pool):
        async with endpoint(tmp_path, pool) as (client, base, proxy, received, key, sink):
            response = await client.request(method, base + path,
                headers={'x-api-key': key, 'Host': f'localhost:{proxy.port}'})
            assert response.status_code == status
            if path == '/v1/models':
                assert [m['id'] for m in response.json()['data']] == ['local-chat']
            assert not received
    run_db(check, timeout=15)


def test_unauthenticated_hello_probe_preserves_counters(tmp_path):
    from copy import deepcopy

    async def check(pool):
        async with endpoint(tmp_path, pool) as (client, base, proxy, received, key, sink):
            before = deepcopy(proxy.stats)
            response = await client.head(base + '/api/hello')
            assert response.status_code == 200 and response.content == b''
            assert proxy.stats == before
            assert not received
            # Ordinary requests still require a key and count real authentication failures.
            response = await client.get(base + '/v1/models')
            assert response.status_code == 401
            assert proxy.stats['auth_failures'] == 1
            assert not received
    run_db(check, timeout=15)


def test_rotated_key_invalidates_existing_proxy_authentication(tmp_path):
    async def check(pool):
        async with endpoint(tmp_path, pool) as (client, base, proxy, received, key, sink):
            assert (await client.get(base + '/v1/models', headers={'x-api-key':key})).status_code == 200
            rotated = GatewayKey(tmp_path).rotate()
            assert (await client.get(base + '/v1/models', headers={'x-api-key':key})).status_code == 401
            assert (await client.get(base + '/v1/models', headers={'x-api-key':rotated})).status_code == 200
            assert not received
            assert key not in json.dumps(proxy.stats) and rotated not in json.dumps(proxy.stats)
    run_db(check, timeout=15)


@pytest.mark.parametrize('case,status', [('unknown',404),('disabled',403),('unavailable',503),('invalid',400),('invalid_alias',404)])
def test_external_resolution_failures(tmp_path, case, status):
    async def check(pool):
        async with endpoint(tmp_path, pool) as (client, base, proxy, received, key, sink):
            if case == 'disabled':
                await pool.execute("UPDATE llm_providers SET enabled=false WHERE id='prov-external'")
            if case == 'unavailable':
                sink.pool = None
            response = await client.post(base + '/v1/messages', headers={'x-api-key':key},
                json=[] if case == 'invalid' else {'model':'absent' if case == 'unknown' else 'a' * 65 if case == 'invalid_alias' else 'local-chat','messages':[]})
            assert response.status_code == status
            if case == 'disabled':
                assert response.json()['error'] == {'type':'permission_error','message':'provider disabled'}
            assert not received
    run_db(check, timeout=15)


def test_external_recording_uses_alias_identity_and_redacts_credentials(tmp_path, caplog):
    caplog.set_level(logging.INFO, logger='looper.agents.llmrec.proxy')
    async def check(pool):
        async with endpoint(tmp_path, pool, record=True) as (client, base, proxy, received, key, sink):
            response = await client.post(base + '/v1/messages', headers={'x-api-key':key,'Authorization':'Bearer '+key},
                                         json={'model':'local-chat','messages':[]})
            assert response.status_code == 200
            await until(lambda: sink.recorded == 1, 'gateway capture', timeout=3)
            route = await pool.fetchrow('SELECT * FROM llm_request_routes')
            assert route['agent_id'] == 'gateway:local-chat'
            assert route['runtime_type'] == 'external'
            row = await pool.fetchrow('SELECT * FROM llm_requests')
            assert key not in json.dumps(dict(row), default=str)
            assert 'upstream-secret' not in json.dumps(dict(row), default=str)
            assert 'kind=translate' in caplog.text
            assert key not in caplog.text and 'upstream-secret' not in caplog.text
    run_db(check, timeout=15)


def test_alias_cache_expires_after_ten_seconds(tmp_path, monkeypatch):
    from looper.agents.llmrec import gateway
    from types import SimpleNamespace
    async def check(pool):
        async with endpoint(tmp_path, pool) as (client, base, proxy, received, key, sink):
            clock = gateway.time.monotonic
            now = clock()
            monkeypatch.setattr(gateway, 'time', SimpleNamespace(monotonic=lambda: now))
            headers = {'x-api-key': key}
            payload = {'model': 'local-chat', 'messages': []}
            assert (await client.post(base + '/v1/messages', headers=headers, json=payload)).status_code == 200
            await pool.execute("UPDATE llm_providers SET enabled=false WHERE id='prov-external'")
            assert (await client.post(base + '/v1/messages', headers=headers, json=payload)).status_code == 200
            now += 10
            assert (await client.post(base + '/v1/messages', headers=headers, json=payload)).status_code == 403
    run_db(check, timeout=15)


@pytest.mark.parametrize('content', [None, '{"format":2,"enabled":true,"record":true}',
                                     '{"format":1,"enabled":false,"record":false}', 'invalid'])
def test_missing_or_invalid_endpoint_config_fails_closed(tmp_path, content):
    async def check(pool):
        async with endpoint(tmp_path, pool) as (client, base, proxy, received, key, sink):
            path = tmp_path / '.local/llmrec/gateway.json'
            assert path.stat().st_mode & 0o777 == 0o600
            if content is None:
                path.unlink()
            else:
                path.write_text(content)
            response = await client.get(base + '/v1/models', headers={'x-api-key':key})
            assert response.status_code == 404
            assert not received
    run_db(check, timeout=15)


@pytest.mark.parametrize('vendor,dialect,field,value', [
    ('deepseek',None,'thinking',{'type':'enabled'}),
    ('deepseek','qwen','enable_thinking',True),
])
def test_alias_model_dialect_overrides_vendor_default(tmp_path, vendor, dialect, field, value):
    async def check(pool):
        async with endpoint(tmp_path, pool) as (client, base, proxy, received, key, sink):
            await pool.execute("UPDATE llm_providers SET vendor_type=$1 WHERE id='prov-external'",vendor)
            await pool.execute("UPDATE llm_models SET chat_dialect=$1 WHERE id='model-external'",dialect)
            response = await client.post(base + '/v1/messages',headers={'x-api-key':key},
                json={'model':'local-chat','messages':[],'thinking':{'type':'enabled'}})
            assert response.status_code == 200
            assert received[0][1].get(field) == value
    run_db(check,timeout=15)


def test_recording_switch_applies_to_next_external_request(tmp_path):
    async def check(pool):
        async with endpoint(tmp_path, pool) as (client, base, proxy, received, key, sink):
            headers = {'x-api-key': key}
            payload = {'model': 'local-chat', 'messages': []}
            GatewayConfig(tmp_path).write(enabled=True, record=True)
            assert (await client.post(base + '/v1/messages', headers=headers, json=payload)).status_code == 200
            await until(lambda: sink.recorded == 1, 'recorded request', timeout=3)
            GatewayConfig(tmp_path).write(enabled=True, record=False)
            assert (await client.post(base + '/v1/messages', headers=headers, json=payload)).status_code == 200
            await until(lambda: proxy.stats['alias_usage']['local-chat']['requests'] == 2,
                        'unrecorded request completion', timeout=2)
            assert sink.recorded == 1 and sink.pending == 0
    run_db(check, timeout=15)
