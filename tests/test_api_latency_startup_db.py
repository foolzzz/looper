"""Partial startup shuts down the HTTP latency observers it already started."""

import asyncio
from contextlib import AsyncExitStack
import threading
from unittest.mock import AsyncMock

import pytest

from looper import db, ops_config
from looper.agents import profile_registry, serve
from tests.db_support import DSN, run_db


@pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")
def test_scheduler_start_failure_closes_listener_and_latency_observers(monkeypatch, tmp_path):
    class StartupFailed(Exception):
        pass

    async def run(pool):
        # No recorder process or runtime probe is needed to reach Scheduler.start.
        await pool.execute("INSERT INTO system_settings (key, value) VALUES ('llmrec.enabled', 'false')")
        monkeypatch.setattr(db, "init_pool", AsyncMock(return_value=pool))
        monkeypatch.setattr(db, "close_pool", AsyncMock())
        monkeypatch.setattr(profile_registry, "register_found_runtimes", AsyncMock(return_value=[]))
        monkeypatch.setattr(serve, "_enabled_bot_rows", AsyncMock(return_value=[]))
        monkeypatch.setattr(serve, "_advisory_startup_checks", AsyncMock())
        monkeypatch.setenv("TZ", "UTC")
        monkeypatch.setattr(serve.AgentSupervisor, "start_all", AsyncMock())
        monkeypatch.setattr(serve.self_check, "check_database", AsyncMock(return_value=
                            serve.self_check.Check("database", "Database", "ok", "ready")))
        observed = {}
        start_watch, start_http = serve.LoopWatch.start, serve.AgentHTTPServer.start

        def watch_started(watch):
            start_watch(watch)
            observed["watch"] = watch
            observed["heartbeat"] = watch._task
            observed["thread"] = watch._thread

        async def http_started(server):
            await start_http(server)
            observed["listener"] = server._server
            observed["summary"] = server._summary_task
            observed["port"] = server._port

        async def scheduler_failed(scheduler):
            # These positive controls prove startup reached the late failure point
            # with all three observers and the real HTTP listener still running.
            assert observed["listener"].is_serving()
            assert not observed["summary"].done()
            assert not observed["heartbeat"].done()
            assert observed["thread"].is_alive()
            observed["scheduler_failed"] = True
            raise StartupFailed("scheduler startup failed")

        monkeypatch.setattr(serve.LoopWatch, "start", watch_started)
        monkeypatch.setattr(serve.AgentHTTPServer, "start", http_started)
        monkeypatch.setattr(serve.Scheduler, "start", scheduler_failed)
        ops = ops_config.OpsConfig(runtime_root=tmp_path, path=tmp_path / "ops.toml", database_url=DSN,
                                   port=0, operator_notes_root=str(tmp_path))
        async with AsyncExitStack() as ownership:
            with pytest.raises(StartupFailed, match="scheduler startup failed"):
                await serve._run_agent_system_owned(ops=ops, runtime_root=tmp_path, work_dir=tmp_path,
                                                     skip_backup=True, ownership=ownership)
            assert observed["scheduler_failed"]
            assert observed["summary"].done()
            assert observed["heartbeat"].done()
            assert not observed["thread"].is_alive()
            assert not observed["listener"].is_serving()
            with pytest.raises(OSError):
                await asyncio.wait_for(asyncio.open_connection("127.0.0.1", observed["port"]), timeout=1)
            assert not any(task.get_name() in {"api-latency-summary", "looper-loop-heartbeat"}
                           for task in asyncio.all_tasks())
            assert observed["thread"] not in threading.enumerate()

    run_db(run, monkeypatch, timeout=30)
