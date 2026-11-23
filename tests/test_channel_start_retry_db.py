"""Startup failures remain observable and recover through the normal channel path."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from looper.agents import serve
from looper.agents.bus import MessageBus
from looper.agents.channels import lark_cli
from looper.agents.channels.lark import LarkChannelAdapter
from looper.agents.channels.registry import ChannelRegistry
from looper.agents.http_server import AgentHTTPServer
from looper.agents.v1_domains import channel_configs as api
from looper.db import channel_configs
from tests.db_support import DSN, run_with_channel_configs

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")


@pytest.mark.parametrize("failure", ["timeout", "network", "credentials", "healthy"])
def test_channel_start_recovers_or_reports_failure(monkeypatch, tmp_path, caplog, failure):
    caplog.set_level("INFO")
    async def test(pool):
        await channel_configs.set_config("channel-lark", {"enabled": True,
            "config": {"allowed_open_ids": ["ou_synthetic00001"]}}, pool=pool)
        attempts = 0
        started = asyncio.Event()
        waits = asyncio.Queue()
        resume = asyncio.Event()
        identity = lark_cli.Identity("", "cli_synthetic", "Synthetic", "ou_bot000000001")

        async def probe(*args, **kwargs):
            nonlocal attempts
            attempts += 1
            if failure != "healthy" and attempts <= 2:
                kind = {"timeout": "timeout", "network": "network", "credentials": "authentication"}[failure]
                raise lark_cli.LarkCliError("synthetic probe failure", error_type=kind)
            return identity

        async def wait(delay):
            await waits.put(delay)
            await resume.wait()
            resume.clear()

        async def start(adapter, bus):
            # This is the normal adapter start boundary: routing was loaded before start.
            assert adapter._resolve_chat(adapter.channel_id, "oc_synthetic").manager_id == "manager"
            adapter._running = True
            started.set()

        monkeypatch.setattr(lark_cli, "identity", probe)
        monkeypatch.setattr(LarkChannelAdapter, "start", start)
        registry = ChannelRegistry(MessageBus(runtime_root=tmp_path))
        monkeypatch.setattr(registry, "_retry_wait", wait, raising=False)
        server = SimpleNamespace(_channels=registry, _self_checks=[], _worker_pool=None)
        principal = SimpleNamespace(kind="operator")

        async def view():
            return await api.run(server, SimpleNamespace(operation_id="channel_configs.get"),
                                 {"channel_id": "channel-lark"}, principal, {})

        try:
            row = await channel_configs.get_config("channel-lark", pool=pool)
            await serve._register_bots(registry, [{**row, "secret": "", "app_id": None}], tmp_path,
                                       lambda _: True, ("lark",))
            if failure != "healthy":
                assert await asyncio.wait_for(waits.get(), 1) == (600 if failure == "credentials" else 30)
                state = (await view())["runtime"]
                assert state["state"] == ("failed" if failure == "credentials" else "retrying")
                assert "synthetic probe failure" in state["last_error"] and state["next_attempt_at"]
                checks = await AgentHTTPServer._checks_now(server)
                assert any(c["id"] == "channel:channel-lark" and state["last_error"] in c["detail"] for c in checks)
                resume.set()
                assert await asyncio.wait_for(waits.get(), 1) == (600 if failure == "credentials" else 60)
                resume.set()
            await asyncio.wait_for(started.wait(), 1)
            assert registry.get("channel-lark") is not None
            assert (await view())["runtime"] == {"state": "running", "last_error": None, "next_attempt_at": None}
            assert await AgentHTTPServer._checks_now(server) == []
            assert attempts == (1 if failure == "healthy" else 3)
            if failure == "healthy":
                assert [record.getMessage() for record in caplog.records if record.name in {
                    "looper.agents.serve", "looper.agents.channels.registry"}] == [
                        "channel-lark: bot Synthetic (app cli_synthetic, lark-cli profile )",
                        "channel channel-lark (lark) registered"]
        finally:
            await registry.stop_all()

    run_with_channel_configs(test, monkeypatch)


@pytest.mark.parametrize("stop", ["disable", "shutdown"])
def test_channel_start_retry_is_cancelled(monkeypatch, tmp_path, stop):
    async def test(pool):
        await channel_configs.set_config("channel-lark", {"enabled": True,
            "config": {"allowed_open_ids": ["ou_synthetic00001"]}}, pool=pool)
        monkeypatch.setattr(lark_cli, "identity", AsyncMock(side_effect=lark_cli.LarkCliError(
            "synthetic timeout", error_type="timeout")))
        registry = ChannelRegistry(MessageBus(runtime_root=tmp_path))
        waiting = asyncio.Event()
        cancelled = asyncio.Event()

        async def wait(delay):
            waiting.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        monkeypatch.setattr(registry, "_retry_wait", wait, raising=False)
        row = await channel_configs.get_config("channel-lark", pool=pool)
        await serve._register_bots(registry, [{**row, "secret": "", "app_id": None}], tmp_path,
                                   lambda _: True, ("lark",))
        await asyncio.wait_for(waiting.wait(), 1)
        if stop == "disable":
            await api.run(SimpleNamespace(_channels=registry), SimpleNamespace(operation_id="channel_configs.update"),
                          {"channel_id": "channel-lark", "fields": {"enabled": False}},
                          SimpleNamespace(kind="operator"), {})
        else:
            registry.prepare_stop_all()
        await asyncio.wait_for(cancelled.wait(), 1)
        assert registry.get("channel-lark") is None
        if stop == "disable":
            assert registry.start_checks() == []
        await registry.stop_all()
        assert lark_cli.identity.await_count == 1

    run_with_channel_configs(test, monkeypatch)


def test_retry_runs_real_adapter_catchup_with_current_bindings(monkeypatch, tmp_path):
    import time

    from looper.db.chat_bindings import put_binding
    from tests.test_lark_catchup import HistoryTransport, history_message, seed_boundary
    from tests.test_lark_transport import OPEN_ID

    async def test(pool):
        await channel_configs.set_config("channel-lark", {"enabled": True,
            "config": {"allowed_open_ids": [OPEN_ID]}}, pool=pool)
        await pool.execute("INSERT INTO managers (id, name, system_prompt) VALUES ('manager-recovery', 'Recovery', 'x')")
        now = time.time()
        await seed_boundary(tmp_path, now - 60)
        transport = HistoryTransport()
        transport.pages["oc_synthetic"] = [{"chat_type": "group", "messages": [
            history_message("om_recovery", at=now - 30)], "has_more": False}]
        original = serve._lark_adapter_from_config

        def build(*args, **kwargs):
            adapter = original(*args, **kwargs)
            adapter._transport.inner = transport  # keep the outbound gate production puts around it
            return adapter

        monkeypatch.setattr(serve, "_lark_adapter_from_config", build)
        monkeypatch.setattr(lark_cli, "identity", AsyncMock(side_effect=[
            lark_cli.LarkCliError("synthetic timeout", error_type="timeout"),
            lark_cli.Identity("", "cli_synthetic", "Synthetic", "ou_helperbot12")]))
        registry = ChannelRegistry(MessageBus(runtime_root=tmp_path))
        waiting, resume = asyncio.Event(), asyncio.Event()

        async def wait(delay):
            waiting.set()
            await resume.wait()

        monkeypatch.setattr(registry, "_retry_wait", wait)
        try:
            row = await channel_configs.get_config("channel-lark", pool=pool)
            await serve._register_bots(registry, [{**row, "secret": "", "app_id": None}], tmp_path,
                                       lambda _: True, ("lark",))
            await asyncio.wait_for(waiting.wait(), 1)
            # A binding saved while the channel was down is loaded on its successful retry.
            async with pool.acquire() as conn, conn.transaction():
                await put_binding("channel-lark", "oc_synthetic", "manager-recovery", conn=conn)
            resume.set()
            await asyncio.wait_for(transport.consumer_started.wait(), 3)
            adapter = registry.get("channel-lark")
            await asyncio.wait_for(adapter._catchup_task, 3)
            messages = registry._bus.journal.undelivered()
            assert len(messages) == 1
            assert messages[0].target.agent_id == "manager-recovery"
            assert messages[0].payload["text"] == "hello"
            assert registry.start_checks() == []
        finally:
            await registry.stop_all()
        assert transport.closed and transport.stream_closed.is_set()

    run_with_channel_configs(test, monkeypatch)


def test_retry_backoff_caps_and_cleans_partial_start(monkeypatch, tmp_path):
    async def test(pool):
        await channel_configs.set_config("channel-lark", {"enabled": True,
            "config": {"allowed_open_ids": ["ou_synthetic00001"]}}, pool=pool)
        identity = lark_cli.Identity("", "cli_synthetic", "Synthetic", "ou_bot000000001")
        monkeypatch.setattr(lark_cli, "identity", AsyncMock(return_value=identity))
        attempts, delays, listeners = [], [], []
        done = asyncio.Event()

        async def start(adapter, bus):
            attempts.append(adapter)
            adapter._running = True
            adapter._collector_task = asyncio.create_task(asyncio.Event().wait())
            listeners.append(adapter._collector_task)
            if len(attempts) < 8:
                raise ConnectionError("synthetic transport failure after allocation")
            done.set()

        async def wait(delay):
            delays.append(delay)
            assert all(task.cancelled() for task in listeners)

        registry = ChannelRegistry(MessageBus(runtime_root=tmp_path))
        monkeypatch.setattr(LarkChannelAdapter, "start", start)
        monkeypatch.setattr(registry, "_retry_wait", wait)
        try:
            row = await channel_configs.get_config("channel-lark", pool=pool)
            await serve._register_bots(registry, [{**row, "secret": "", "app_id": None}], tmp_path,
                                       lambda _: True, ("lark",))
            await asyncio.wait_for(done.wait(), 3)
            assert delays == [30, 60, 120, 240, 480, 600, 600]
            assert registry.get("channel-lark") is attempts[-1]
            assert registry.start_checks() == []
        finally:
            await registry.stop_all()
        assert all(task.cancelled() for task in listeners)

    run_with_channel_configs(test, monkeypatch)


def test_serve_http_reports_retry_and_recovery(monkeypatch, tmp_path):
    import httpx

    from looper import db, ops_config
    from looper.agents import api_v1, operator_notes, profile_registry
    from looper.agents.channels.down_alerts import Monitor
    from looper.agents.channels.telegram import TelegramChannelAdapter
    from tests.test_lark_catchup import HistoryTransport

    async def test(pool):
        from datetime import datetime, timedelta, timezone

        clock = [datetime.now(timezone.utc)]
        sent = []
        await channel_configs.set_config("channel-lark", {"enabled": True,
            "config": {"allowed_open_ids": ["ou_synthetic00001"]}}, pool=pool)
        await channel_configs.set_config("channel-telegram", {"enabled": True,
            "config": {"allowed_user_ids": ["1000001"], "operator_sender_id": "1000001"}}, pool=pool)
        await channel_configs.set_bot_token("channel-telegram", "0:synthetic", pool=pool)
        await pool.execute("INSERT INTO system_settings (key, value) VALUES ('llmrec.enabled', 'false') "
                           "ON CONFLICT (key) DO UPDATE SET value = 'false'")
        identity = lark_cli.Identity("", "cli_synthetic", "Synthetic", "ou_bot000000001")
        monkeypatch.setattr(lark_cli, "identity", AsyncMock(side_effect=[
            lark_cli.LarkCliError("synthetic enumeration timeout", error_type="timeout"),
            lark_cli.LarkCliError("synthetic probe timeout", error_type="timeout"),
            lark_cli.LarkCliError("synthetic probe timeout", error_type="timeout"), identity]))
        transport = HistoryTransport()
        original = serve._lark_adapter_from_config
        resume = asyncio.Event()
        servers = []

        def build(*args, **kwargs):
            adapter = original(*args, **kwargs)
            # Replace the transport inside the outbound gate, as production wraps every Lark transport.
            adapter._transport.inner = transport
            return adapter

        async def wait(delay):
            await resume.wait()
            await asyncio.sleep(0)  # yield, so shutdown can cancel a retry loop that keeps failing

        async def telegram_start(adapter, bus):
            async def send(**kwargs):
                sent.append(kwargs)
                return SimpleNamespace(message_id=len(sent))
            adapter._app = SimpleNamespace(bot=SimpleNamespace(send_message=send))
            adapter._running = True

        async def telegram_stop(adapter):
            adapter._running = False

        http_start = AgentHTTPServer.start

        async def start_http(server):
            servers.append(server)
            await http_start(server)

        async def check_http(supervisor):
            server = servers[0]
            port = server._server.sockets[0].getsockname()[1]
            assert port != 8767
            async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}", timeout=5, trust_env=False,
                    headers={"Authorization": f"Bearer {server._operator_token}"}) as client:
                async def call(operation, body=None):
                    response = await client.post("/api/v1/" + operation.replace(".", "/"), json=body or {})
                    assert response.status_code == 200, response.text
                    result = response.json()["result"]
                    api_v1.validate_arguments(api_v1.find("POST", response.request.url.path).result, result)
                    return result

                status = await call("system.status")
                assert any(c["id"] == "channel:channel-lark" for c in status["checks"])
                config = await call("channel_configs.get", {"channel_id": "channel-lark"})
                assert config["runtime"]["state"] == "retrying"
                monitor = Monitor(server._channels, clock=lambda: clock[0])
                await monitor.tick()
                clock[0] += timedelta(minutes=11)
                await monitor.tick()
                await monitor.tick()
                assert any(c["id"] == "channel_down:channel-lark" for c in (await call("system.status"))["checks"])
                assert len(sent) == 1 and sent[0]["chat_id"] == 1000001
                resume.set()
                await asyncio.wait_for(transport.consumer_started.wait(), 5)
                config = await call("channel_configs.get", {"channel_id": "channel-lark"})
                assert config["runtime"]["state"] == "running"
                await monitor.tick()
                await monitor.tick()
                assert len(sent) == 2 and "恢复" in sent[-1]["text"]
                assert not any(c["id"].startswith("channel_down:") for c in (await call("system.status"))["checks"])
                assert not any(c["id"] == "channel:channel-lark" for c in (await call("system.status"))["checks"])

        monkeypatch.setattr(serve, "_lark_adapter_from_config", build)
        monkeypatch.setattr(ChannelRegistry, "_retry_wait", staticmethod(wait))
        monkeypatch.setattr(TelegramChannelAdapter, "start", telegram_start)
        monkeypatch.setattr(TelegramChannelAdapter, "stop", telegram_stop)
        monkeypatch.setattr(AgentHTTPServer, "start", start_http)
        monkeypatch.setattr(serve.AgentSupervisor, "start_all", check_http)
        monkeypatch.setattr(operator_notes, "_root", str(tmp_path))
        monkeypatch.setattr(db, "init_pool", AsyncMock(return_value=pool))
        monkeypatch.setattr(db, "close_pool", AsyncMock())
        monkeypatch.setattr(profile_registry, "register_found_runtimes", AsyncMock(return_value=[]))
        ops = ops_config.OpsConfig(runtime_root=tmp_path, path=tmp_path / "ops.toml", database_url=DSN,
                                   port=0, operator_notes_root=str(tmp_path))
        # Exercise the normal service shutdown, without sending a signal to the test worker.
        import signal
        loop = asyncio.get_running_loop()
        monkeypatch.setattr(loop, "add_signal_handler", lambda sig, handler:
                            loop.call_soon(handler) if sig == signal.SIGTERM else None)
        await serve._run_agent_system(ops=ops, runtime_root=tmp_path, work_dir=tmp_path, skip_backup=True,
                                      runner_supervisor=SimpleNamespace(stop=AsyncMock()))
        assert transport.closed
        assert all(not task.get_name().startswith("channel-start:") for task in asyncio.all_tasks())
        assert all(task.get_name() != "channel-health" for task in asyncio.all_tasks())

    run_with_channel_configs(test, monkeypatch)
