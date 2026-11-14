"""Status polls read advisory caches without waiting for login or proxy probes."""
import asyncio
import threading
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from looper.agents import self_check
from looper.agents.http_server import AgentHTTPServer
from looper.agents.llmrec import control, settings
from looper.agents.runtimes import codex_login, coco_login, cursor_login
from looper.db import connection, llm_requests, profile_registry, system_settings, tasks


@pytest.fixture
def server(tmp_path, monkeypatch):
    monkeypatch.setattr(tasks, 'status_counts', AsyncMock(return_value=[]))
    monkeypatch.setattr(profile_registry, 'login_profile_count', AsyncMock(return_value=1))
    monkeypatch.setattr(self_check, '_ttadk_runtime_rows', AsyncMock(return_value=[]))
    monkeypatch.setattr(self_check, '_translated_profile_rows', AsyncMock(return_value=[
        {'type': 'claude-code', 'protocol': 'openai-chat'}]))
    monkeypatch.setattr(system_settings, 'get_settings', AsyncMock(return_value={}))
    monkeypatch.setattr(connection, 'get_pool', lambda: http)
    monkeypatch.setattr(llm_requests, 'logical_bytes', AsyncMock(return_value=0))
    monkeypatch.setattr(settings, 'max_total_mb', AsyncMock(return_value=100))
    for module in (codex_login, coco_login, cursor_login):
        module.forget_probe()
        if hasattr(module, 'recorded_problem'):
            monkeypatch.setattr(module, 'recorded_problem', AsyncMock(return_value=None))
    # Grok's file check is outside this regression and must not read a machine login.
    monkeypatch.setitem(self_check.LOGIN_CHECKS, 'grok_login', AsyncMock(return_value=
        self_check.Check('grok_login', 'Grok login', 'skip', 'unused')))
    owner = control.ProxyControl(tmp_path, '', enabled=False)
    monkeypatch.setattr(control, '_control', owner)
    http = AgentHTTPServer(port=0, operator_token="status-test-token", runtime_root=tmp_path, static_dir=tmp_path,
        supervisor=SimpleNamespace(agent_status=lambda: {}), task_router=SimpleNamespace(),
        web_adapter=SimpleNamespace(healthcheck=lambda: {"channel": "web", "healthy": True, "active_sse_streams": 0}), self_checks=[
            self_check.Check(key, key, 'pending', 'checking').to_dict() for key in self_check.LOGIN_CHECKS])
    yield http, owner
    for module in (codex_login, coco_login, cursor_login):
        module.forget_probe()


def test_status_returns_during_slow_probes_and_publishes_their_results(server, monkeypatch):
    http, owner = server
    probes = []
    threads = []
    completed = []
    for module, state in ((codex_login, codex_login.LoginState.CHATGPT),
                          (coco_login, coco_login.LoginState.NOT_LOGGED_IN),
                          (cursor_login, cursor_login.LoginState.LOGGED_IN)):
        done = threading.Event()
        def probe(*args, module=module, state=state, done=done):
            threads.append(threading.current_thread())
            completed.append(done)
            probes.append(module.__name__)
            try:
                time.sleep(5)
                return module.LoginStatus(state, 'synthetic result')
            finally:
                done.set()
        monkeypatch.setattr(module, 'probe_login', probe)
    async def slow_running(state):
        await asyncio.to_thread(time.sleep, 5)
        raise AssertionError('status must not inspect processes')
    monkeypatch.setattr(owner, '_running', slow_running)

    async def run():
        before = time.monotonic()
        try:
            results = await asyncio.wait_for(asyncio.gather(
                *(http._status_result({}) for _ in range(10))), 1.5)
            elapsed = time.monotonic() - before
            print(f'cold concurrent status latency: {elapsed:.3f}s')
            assert all(c['status'] == 'pending' for result in results for c in result['checks']
                       if c['id'] in ('codex_login', 'coco_login', 'cursor_login'))
            await http._status_result({})
        finally:
            # Join every synthetic probe before fixture cleanup or interpreter shutdown.
            for done in completed:
                assert await asyncio.to_thread(done.wait, 6)
            for thread in threads:
                if thread.daemon:
                    await asyncio.to_thread(thread.join, 1)
                    assert not thread.is_alive()
        result = await http._status_result({})
        checks = {c['id']: c for c in result['checks']}
        assert [checks[key]['status'] for key in ('codex_login', 'coco_login', 'cursor_login')] == ['ok', 'fail', 'ok']
        assert sorted(probes) == sorted(module.__name__ for module in (codex_login, coco_login, cursor_login))
    asyncio.run(run())


def test_proxy_health_is_read_once_and_storage_is_cached(server, monkeypatch):
    http, owner = server
    http._self_checks = [c for c in http._self_checks if c['id'] in ('llmrec', 'gateway')]
    health = AsyncMock(return_value={'features': {'translate': 1}})
    monkeypatch.setattr(owner, '_health', health)
    async def run():
        for _ in range(2):
            await http._checks_now()
        assert health.await_count == 2
        assert all(call.kwargs == {'cached': True} for call in health.await_args_list)
        assert llm_requests.logical_bytes.await_count == 1
        assert settings.max_total_mb.await_count == 1
    asyncio.run(run())


def test_startup_status_returns_before_queued_blocking_startup_work(server):
    http, _ = server
    http._self_checks = [self_check.Check('database', 'PostgreSQL', 'ok', 'connected').to_dict(), *[
        self_check.Check(key, key, 'pending', 'Startup check in progress').to_dict()
        for key in self_check.LOGIN_CHECKS]]

    async def run():
        # Startup can still be loading modules synchronously after HTTP begins listening.
        queued_startup = asyncio.get_running_loop().call_soon(time.sleep, 2)
        before = time.monotonic()
        try:
            result = await http._status_result({})
            assert time.monotonic() - before < 1.5
            assert result['checks'] == http._self_checks
        finally:
            queued_startup.cancel()

    asyncio.run(run())


def test_check_budget_keeps_previous_result_and_does_not_cancel_or_duplicate(server, monkeypatch):
    http, _ = server
    original = [self_check.Check(key, key, 'ok', 'startup answer').to_dict() for key in ('codex_login', 'cursor_login')]
    http._self_checks = original
    async def run():
        for key in ('codex_login', 'cursor_login'):
            monkeypatch.setitem(self_check.LOGIN_CHECKS, key, AsyncMock(return_value=
                self_check.Check(key, key, 'ok', 'last answer')))
        await http._checks_now()
        release = asyncio.Event()
        started = []
        async def check(key):
            started.append(key)
            await release.wait()
            return self_check.Check(key, key, 'fail', 'fresh answer')
        for key in ('codex_login', 'cursor_login'):
            monkeypatch.setitem(self_check.LOGIN_CHECKS, key, lambda key=key: check(key))
        before = time.monotonic()
        try:
            result = await http._checks_now()
            elapsed = time.monotonic() - before
            assert elapsed < 1.5
            print(f'timed out checks returned cached answers: {elapsed:.3f}s')
            assert len(started) == 2
            assert all(c['status'] == 'ok' and c['detail'] == 'last answer (still checking)' for c in result)
            again = asyncio.create_task(http._checks_now())
            await asyncio.sleep(0)
            assert len(started) == 2
            release.set()
            assert all(c['status'] == 'fail' for c in await again)
        finally:
            release.set()
            await asyncio.sleep(0)
    asyncio.run(run())


@pytest.mark.parametrize('age,expected', [(10, 'running'), (16, 'unavailable')])
def test_cached_proxy_status_never_falls_back_to_process_or_http(server, monkeypatch, age, expected):
    _, owner = server
    owner.enabled = True
    owner._health_at = time.monotonic() - age
    owner._health_cache = {'features': {'translate': 1}}
    running = AsyncMock(side_effect=AssertionError('no process probe from cached status'))
    monkeypatch.setattr(owner, '_running', running)
    result = asyncio.run(owner.status(cached=True))
    assert result['proxy']['state'] == expected
    running.assert_not_awaited()


@pytest.mark.parametrize('module', [codex_login, coco_login, cursor_login])
def test_stale_login_remains_available_while_refresh_and_launch_share_one_probe(server, monkeypatch, module):
    previous = module.LoginStatus(module.LoginState.NOT_LOGGED_IN, 'old answer')
    latest = module.LoginStatus(codex_login.LoginState.CHATGPT if module is codex_login
                                else module.LoginState.LOGGED_IN, 'new answer')
    stale = (time.monotonic() - 31, previous)
    monkeypatch.setattr(module, '_probed', {'coco': stale} if module is coco_login else stale)
    entered, release = threading.Event(), threading.Event()
    threads = []
    def probe(*args):
        threads.append(threading.current_thread())
        entered.set()
        assert release.wait(3)
        return latest
    monkeypatch.setattr(module, 'probe_login', probe)
    async def run():
        waiter = None
        try:
            assert module.cached_status() == previous
            assert await asyncio.to_thread(entered.wait, 1)
            waiter = asyncio.create_task(asyncio.to_thread(module.cached_probe))
            for _ in range(10):
                assert module.cached_status() == previous
            assert len(threads) == 1
        finally:
            release.set()
            if waiter is not None:
                assert await waiter == latest
            for thread in threads:
                await asyncio.to_thread(thread.join, 1)
                assert not thread.is_alive()
        assert module.cached_status() == latest
    asyncio.run(run())


def test_storage_expiry_refreshes_usage_and_cap_together(server, monkeypatch):
    http, _ = server
    http._self_checks = [c for c in http._self_checks if c['id'] == 'llmrec']
    clock = [100.0]
    monkeypatch.setattr(self_check, 'time', SimpleNamespace(monotonic=lambda: clock[0]))
    async def run():
        await http._checks_now()
        llm_requests.logical_bytes.return_value = 2 * 1024 * 1024
        settings.max_total_mb.return_value = 1
        clock[0] += 59
        assert (await http._checks_now())[0]['status'] == 'skip'
        clock[0] += 1
        assert (await http._checks_now())[0]['status'] == 'warn'
        assert llm_requests.logical_bytes.await_count == 2
        assert settings.max_total_mb.await_count == 2
    asyncio.run(run())


def test_status_http_operation_returns_cached_checks(server, monkeypatch):
    http, owner = server
    http._self_checks = [c for c in http._self_checks if c['id'] in ('llmrec', 'gateway')]
    monkeypatch.setattr(owner, '_running', AsyncMock(side_effect=AssertionError('must use cache')))
    async def run():
        await http.start()
        try:
            assert http._port != 8767
            async with httpx.AsyncClient(trust_env=False, timeout=1.5) as client:
                before = time.monotonic()
                response = await client.post(f'http://127.0.0.1:{http._port}/api/v1/system/status',
                    headers={'Authorization': 'Bearer status-test-token'}, json={})
                elapsed = time.monotonic() - before
            assert response.status_code == 200, response.text
            answer = response.json()
            assert answer['status'] == 'ok', answer
            assert {c['id'] for c in answer['result']['checks']} == {'llmrec', 'gateway'}
            assert elapsed < 1.5
            print(f'HTTP cached status latency: {elapsed:.3f}s')
        finally:
            await http.stop()
    asyncio.run(run())


def test_failed_check_keeps_last_answer_and_recovers_on_next_poll(server, monkeypatch, caplog):
    http, _ = server
    http._self_checks = [self_check.Check('codex_login', 'Codex login', 'warn', 'last answer').to_dict()]
    check = AsyncMock(side_effect=[RuntimeError('synthetic failure'),
        self_check.Check('codex_login', 'Codex login', 'ok', 'new answer')])
    monkeypatch.setitem(self_check.LOGIN_CHECKS, 'codex_login', check)
    async def run():
        assert await http._checks_now() == http._self_checks
        assert 'status check failed: codex_login' in caplog.text
        assert (await http._checks_now())[0]['status'] == 'ok'
    asyncio.run(run())


def test_periodic_proxy_tick_warms_the_status_cache(server, monkeypatch):
    _, owner = server
    owner.gateway_enabled = True
    start = AsyncMock(side_effect=AssertionError('healthy proxy must not restart'))
    monkeypatch.setattr(owner, '_start', start)
    state = {'pid': 1234, 'instance': 'synthetic', 'start_time': 'synthetic', 'port': 12345}
    monkeypatch.setattr(owner, '_state', lambda: state)
    running = AsyncMock(return_value=True)
    monkeypatch.setattr(owner, '_running', running)
    # Earlier health checks may already have populated the process-wide fingerprint.
    monkeypatch.setattr(control, '_code_hash', 'synthetic-code')
    client = httpx.AsyncClient
    requests = []
    def health(request):
        requests.append(request)
        return httpx.Response(200, json={**state, 'code_sha256': 'synthetic-code',
            'route_format': control.ROUTE_FORMAT, 'features': {'translate': 1}, 'recorded': 7})
    monkeypatch.setattr(httpx, 'AsyncClient', lambda **kwargs: client(**kwargs, transport=httpx.MockTransport(health)))
    async def run():
        await owner.tick()
        status = await owner.status(cached=True)
        assert status['proxy']['state'] == 'running'
        assert status['proxy']['recorded'] == 7
        assert len(requests) == 1
        running.assert_awaited_once()
        start.assert_not_awaited()
    asyncio.run(run())
