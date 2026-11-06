"""Exercise recorder ownership through its actual isolated subprocess boot."""

import asyncio
import os
import signal
from unittest.mock import AsyncMock, Mock
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import httpx
import pytest

from looper.agents.keeper import write_json
from looper.agents.llmrec import ROUTES_DIR, STATE_FILE, RouteFile, SkipReason
from looper.agents.llmrec import control as module
from looper.agents.llmrec.control import ProxyControl
from looper.db import llm_records
from tests.db_support import DSN, run_db
from tests.fake_model_api import FakeModelAPI, ResponseScript

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")


async def wait_for(check, timeout=8):
    deadline = asyncio.get_running_loop().time() + timeout
    while not await check():
        assert asyncio.get_running_loop().time() < deadline, "recorder lifecycle timed out"
        await asyncio.sleep(.05)


async def cleanup(control):
    state = control._state()
    if state:
        await control._stop(state)
    # Reap children killed by a test, even after their state file was removed.
    for child in list(control._children.values()):
        if child.returncode is None:
            child.terminate()
        await asyncio.wait_for(child.wait(), 5)


async def started(control, kept=()):
    await control.at_start(kept)
    # Launch callers bypass after three seconds; a cold process may finish booting later.
    await wait_for(control._health)


@pytest.fixture(autouse=True)
def _gateway_settings(monkeypatch):
    # These process-lifecycle cases supply a DSN without owning the application pool.
    # Endpoint settings persistence is exercised with the shared schema fixture separately.
    from looper.db import system_settings

    monkeypatch.setattr(system_settings, "get_settings", AsyncMock(return_value={}))


@pytest.fixture
def short_checks(monkeypatch):
    boot = module.boot_argv
    monkeypatch.setattr(module, "boot_argv", lambda *args: [*boot(*args), "--check-interval", "1"])


@pytest.mark.parametrize("kept", [False, True])
def test_boot_reuse_and_upgrade(tmp_path, short_checks, kept):
    async def run():
        first = ProxyControl(tmp_path, DSN, enabled=True)
        second = ProxyControl(tmp_path, DSN, enabled=True)
        try:
            await started(first)
            initial = first._state()
            assert initial and await first._health()
            await second.at_start([])
            assert second._state()["pid"] == initial["pid"]
            changed = {**initial, "code_sha256": "old-code"}
            write_json(tmp_path / STATE_FILE, changed)
            route = RouteFile(id="a" * 22, upstream="http://127.0.0.1:1", agent_id="worker",
                              created_at="2026-01-01T00:00:00+00:00", record_id=42)
            route.write(tmp_path / ROUTES_DIR / f"{route.id}.json")
            await second.at_start([42] if kept else [])
            await wait_for(second._health)
            final = second._state()
            assert final["port"] == initial["port"]
            assert (final["pid"] == initial["pid"]) is kept
            assert (final["instance"] == initial["instance"]) is kept
            reconciled = RouteFile.read(tmp_path / ROUTES_DIR / f"{route.id}.json")
            assert reconciled.record_id == (42 if kept else None)
            assert bool(reconciled.ended_at) is not kept
        finally:
            await cleanup(second)
            await cleanup(first)
    asyncio.run(asyncio.wait_for(run(), 25))


def test_crash_restarts_on_original_port(tmp_path, short_checks):
    async def run():
        control = ProxyControl(tmp_path, DSN, enabled=True)
        try:
            await started(control)
            first = control._state()
            os.kill(first["pid"], signal.SIGKILL)
            await control._children[first["pid"]].wait()
            async def recover():
                await control.tick()
                await wait_for(control._health)
            await asyncio.wait_for(recover(), 5)
            second = control._state()
            assert second["pid"] != first["pid"]
            assert second["port"] == first["port"]
            assert await control._health()
        finally:
            await cleanup(control)
    asyncio.run(asyncio.wait_for(run(), 20))


@pytest.mark.parametrize('replacement_fails', [False, True])
def test_failed_writer_replaced_on_original_port_and_records_again(tmp_path, short_checks, monkeypatch, replacement_fails):
    from looper.agents import self_check

    async def run(pool):
        schema = await pool.fetchval('SELECT current_schema()')
        url = urlsplit(DSN)
        scoped_dsn = urlunsplit(url._replace(query=urlencode([*parse_qsl(url.query), ('search_path', schema)])))
        control = ProxyControl(tmp_path, scoped_dsn, enabled=True)
        monkeypatch.setattr(module, '_control', control)
        script = ResponseScript(events=[{'type': 'message_start', 'message': {
            'id': 'msg_test', 'type': 'message', 'role': 'assistant', 'content': [],
            'usage': {'input_tokens': 1, 'output_tokens': 0}}}, {'type': 'message_stop'}])
        try:
            await started(control)
            first = control._state()
            await pool.execute("ALTER TABLE llm_requests ADD CONSTRAINT reject_capture CHECK (model <> 'reject-writer')")
            async with FakeModelAPI([script, script, script]) as upstream:
                route = RouteFile(id='d' * 22, upstream=upstream.base_url, agent_id='worker-recovery',
                    protocol='anthropic-messages', created_at='2026-01-01T00:00:00+00:00')
                route.write(tmp_path / ROUTES_DIR / f'{route.id}.json')
                async with httpx.AsyncClient(trust_env=False, timeout=3) as client:
                    endpoint = f"http://127.0.0.1:{first['port']}"

                    async def send(model):
                        response = await client.post(f'{endpoint}/r/{route.id}/messages',
                            json={'model': model, 'messages': [], 'stream': True},
                            headers={'authorization': 'Bearer test-key'})
                        assert response.status_code == 200 and b'message_stop' in response.content

                    async def written():
                        return await pool.fetchval('SELECT count(*) FROM llm_requests') == 1

                    await send('before-failure')
                    await wait_for(written)
                    await send('reject-writer')

                    async def failed():
                        response = await client.get(f'{endpoint}/llmrec/health')
                        return response.status_code == 503

                    await wait_for(failed)
                    status = (await control.status())['proxy']
                    assert status['state'] == 'unavailable'
                    assert status['write_errors'] == 1 and status['recorded'] == 1
                    check = await self_check.llmrec_check()
                    assert check.status == self_check.FAIL and 'write_errors=1' in check.detail
                    await pool.execute('ALTER TABLE llm_requests DROP CONSTRAINT reject_capture')
                    if replacement_fails:
                        with monkeypatch.context() as patch:
                            patch.setattr(asyncio, 'create_subprocess_exec', AsyncMock(side_effect=OSError('boot failed')))
                            with pytest.raises(OSError, match='boot failed'):
                                await control.tick()
                        status = (await control.status())['proxy']
                        assert status['state'] == 'unavailable' and status['write_errors'] == 1
                        assert status['port'] == first['port']
                    await control.tick()
                    await wait_for(control._health)
                    second = control._state()
                    assert second['pid'] != first['pid'] and second['instance'] != first['instance']
                    assert second['port'] == first['port']
                    assert not await control._running(first)
                    await send('after-recovery')

                    async def recovered():
                        return await pool.fetchval("SELECT count(*) FROM llm_requests WHERE model='after-recovery'") == 1

                    await wait_for(recovered)
                    assert (await control.status())['proxy']['recorded'] == 1
                    assert len(upstream.requests) == 3
        finally:
            await cleanup(control)

    run_db(run, timeout=35)


def test_state_removal_ends_proxy(tmp_path, short_checks):
    async def run():
        control = ProxyControl(tmp_path, DSN, enabled=True)
        try:
            await started(control)
            state = control._state()
            (tmp_path / STATE_FILE).unlink()
            child = control._children[state["pid"]]
            assert await asyncio.wait_for(child.wait(), 5) == 0
        finally:
            await cleanup(control)
    asyncio.run(asyncio.wait_for(run(), 15))


def test_unreachable_database_does_not_gate_forwarding_or_expose_dsn(tmp_path, short_checks):
    async def run():
        dsn = "postgresql://postgres:synthetic-secret@127.0.0.1:54329/looper_llmrec_unreachable"
        control = ProxyControl(tmp_path, dsn, enabled=True)
        try:
            await started(control)
            state = control._state()
            assert state is not None
            script = ResponseScript(events=[{"type": "message_start", "message": {
                "id": "msg_test", "type": "message", "role": "assistant", "content": [],
                "usage": {"input_tokens": 1, "output_tokens": 0}}},
                {"type": "message_stop"}])
            async with FakeModelAPI([script]) as upstream:
                route = RouteFile(id="b" * 22, upstream=upstream.base_url, agent_id="worker",
                                  created_at="2026-01-01T00:00:00+00:00")
                route.write(tmp_path / ROUTES_DIR / f"{route.id}.json")
                async with httpx.AsyncClient(trust_env=False) as client:
                    response = await client.post(f"http://127.0.0.1:{state['port']}/r/{route.id}/messages",
                        json={"model": "test", "messages": [], "stream": True},
                        headers={"authorization": "Bearer test-key"})
                assert response.status_code == 200
                assert b"message_start" in response.content
                assert len(upstream.requests) == 1
            async def errors():
                health = await control._health()
                return health and health["write_errors"] > 0
            await wait_for(errors)
            for path in tmp_path.rglob("*"):
                if path.is_file():
                    contents = path.read_bytes()
                    assert dsn.encode() not in contents
                    assert b"synthetic-secret" not in contents
        finally:
            await cleanup(control)
    asyncio.run(asyncio.wait_for(run(), 25))


def test_switch_off_preserves_running_proxy_until_safe_start(tmp_path, short_checks):
    async def run():
        control = ProxyControl(tmp_path, DSN, enabled=True)
        try:
            await started(control)
            first = control._state()
            await control.set_enabled(False)
            assert await control._health()
            await control.at_start([42])
            assert control._state()["pid"] == first["pid"]
            assert (await control.status())["proxy"]["state"] == "running"
            await control.at_start([])
            assert control._state() is None
            assert not await control._running(first)
            assert (await control.status())["proxy"]["state"] == "stopped"
        finally:
            await cleanup(control)
    asyncio.run(asyncio.wait_for(run(), 20))


def test_held_stream_survives_control_plane_adoption_and_keeps_record(tmp_path, short_checks):
    async def run(pool):
        schema = await pool.fetchval("SELECT current_schema()")
        url = urlsplit(DSN)
        scoped_dsn = urlunsplit(url._replace(query=urlencode([*parse_qsl(url.query), ("search_path", schema)])))
        control = ProxyControl(tmp_path, scoped_dsn, enabled=True)
        adopted = ProxyControl(tmp_path, scoped_dsn, enabled=True)
        request = None
        gate = asyncio.Event()
        script = ResponseScript(events=[
            {"type": "message_start", "message": {"id": "msg_test", "type": "message", "role": "assistant",
                "model": "test", "content": [], "usage": {"input_tokens": 3, "output_tokens": 0}}},
            {"type": "response.completed", "response": {"output": []}},
            {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 2}},
            {"type": "message_stop"}], gate=gate)
        try:
            await started(control)
            first = control._state()
            assert first is not None
            record_id = await llm_records.start_record(agent_id="worker-adoption", pool=pool)
            async with FakeModelAPI([script]) as upstream:
                route = RouteFile(id="c" * 22, upstream=upstream.base_url, agent_id="worker-adoption",
                    protocol="anthropic-messages", created_at="2026-01-01T00:00:00+00:00", record_id=record_id)
                route.write(tmp_path / ROUTES_DIR / f"{route.id}.json")
                async with httpx.AsyncClient(trust_env=False, timeout=15) as client:
                    request = asyncio.create_task(client.post(
                        f"http://127.0.0.1:{first['port']}/r/{route.id}/messages",
                        json={"model": "test", "stream": True, "messages": [{"role": "user", "content": "hi"}]},
                        headers={"authorization": "Bearer test-key"}))
                    await asyncio.wait_for(script.gate_reached.wait(), 5)
                    assert not request.done()
                    await adopted.at_start([record_id])
                    assert adopted._state()["pid"] == first["pid"]
                    assert adopted._state()["instance"] == first["instance"]
                    gate.set()
                    response = await request
                    assert response.status_code == 200 and b"message_stop" in response.content
                assert RouteFile.read(tmp_path / ROUTES_DIR / f"{route.id}.json").record_id == record_id
            async def written():
                return await pool.fetchval("SELECT count(*) FROM llm_requests WHERE record_id=$1", record_id) == 1
            await wait_for(written)
            row = await pool.fetchrow("SELECT outcome,input_tokens,output_tokens FROM llm_requests WHERE record_id=$1", record_id)
            assert dict(row) == {"outcome": "completed", "input_tokens": 3, "output_tokens": 2}
        finally:
            gate.set()
            if request is not None and not request.done():
                request.cancel()
                await asyncio.gather(request, return_exceptions=True)
            await cleanup(adopted)
            await cleanup(control)
    run_db(run, timeout=30)


@pytest.mark.parametrize("upstream", ["https://user:secret@model.invalid", "https://model.invalid?api_key=value",
                                     "https://model.invalid?auth=", "https://model.invalid?session_token=value"])
def test_credentials_in_upstream_never_start_proxy(tmp_path, monkeypatch, upstream):
    from looper.agents.agent_settings import Launch

    launch = Launch()
    monkeypatch.setattr(type(launch.runtime), "llmrec_upstream", lambda self, launch: upstream)
    control = ProxyControl(tmp_path, DSN, enabled=True, include_oauth=True)
    async def run():
        attached = await control.attach(launch, agent_id="worker", task_id=None)
        assert attached.llmrec.reason is SkipReason.CREDENTIALS_IN_URL
        assert control._state() is None
    asyncio.run(run())


def test_kept_proxy_route_format_mismatch_skips_new_launch(tmp_path, monkeypatch, short_checks):
    from looper.agents.agent_settings import Launch

    launch = Launch()
    monkeypatch.setattr(type(launch.runtime), "llmrec_upstream", lambda self, launch: "https://model.invalid")
    async def run():
        control = ProxyControl(tmp_path, DSN, enabled=True, include_oauth=True)
        try:
            await started(control)
            original = control._state()
            monkeypatch.setattr(module, "ROUTE_FORMAT", module.ROUTE_FORMAT + 1)
            await control.at_start([42])
            assert control._state()["pid"] == original["pid"]
            attached = await control.attach(launch, agent_id="worker", task_id=None)
            assert attached.llmrec.reason is SkipReason.PROXY_OUTDATED
            assert (await control.status())["proxy"]["state"] == "outdated"
        finally:
            await cleanup(control)
    asyncio.run(asyncio.wait_for(run(), 15))


@pytest.mark.parametrize("failure", ["missing_state", "route_write"])
def test_attach_filesystem_races_bypass_without_affecting_launch(tmp_path, monkeypatch, caplog, failure):
    from looper.agents.agent_settings import AgentSettings, Launch
    from looper.agents.profile_registry import builtin_resolved_profile

    launch = Launch(AgentSettings(profile=builtin_resolved_profile()))
    control = ProxyControl(tmp_path, DSN, enabled=True, include_oauth=True)
    monkeypatch.setattr(module, "_control", control)
    monkeypatch.setattr(type(launch.runtime), "llmrec_upstream", lambda self, launch: "https://model.invalid")
    async def healthy(**kwargs):
        return {"instance": "test", "route_format": 1}
    monkeypatch.setattr(control, "_health", healthy)
    monkeypatch.setattr(control, "_state", lambda: None if failure == "missing_state" else {"port": 1234})
    denied = Mock(side_effect=OSError("sensitive filesystem error"))
    monkeypatch.setattr(RouteFile, "write", denied)
    async def run():
        attached = await module.attach(launch, agent_id="worker", task_id=None)
        assert attached.llmrec.reason is SkipReason.PROXY_UNAVAILABLE
        assert attached.settings is launch.settings
        assert "sensitive filesystem error" not in caplog.text
        assert denied.call_count == int(failure == "route_write")
    asyncio.run(run())


def test_enabling_recording_retains_setting_when_start_fails(tmp_path, monkeypatch, caplog):
    control = ProxyControl(tmp_path, DSN, enabled=False)
    async def unavailable():
        raise OSError("sensitive connection error")
    monkeypatch.setattr(control, "_start", unavailable)
    async def run():
        await control.set_enabled(True, include_oauth=True)
        assert control.enabled and control.include_oauth
        assert (await control.status())["proxy"]["state"] == "unavailable"
        assert "sensitive connection error" not in caplog.text
    asyncio.run(run())
