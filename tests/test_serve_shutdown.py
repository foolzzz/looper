"""How ``looper serve`` stops: the first SIGINT or SIGTERM starts the shutdown, a shutdown that runs past its deadline
exits at once, and so does a second signal."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from looper.agents import serve


def test_a_shutdown_that_ends_in_time_exits_normally() -> None:
    exits: list[int] = []

    async def shut_down() -> None:
        await asyncio.sleep(0.05)

    asyncio.run(serve.shut_down_within(shut_down(), deadline=1.0, exit_now=exits.append))
    assert exits == []


def test_a_shutdown_past_its_deadline_exits_at_once() -> None:
    exits: list[int] = []

    async def _run() -> float:
        async def hangs() -> None:
            await asyncio.Event().wait()  # an agent that never stops

        began = asyncio.get_running_loop().time()
        await serve.shut_down_within(hangs(), deadline=0.2, exit_now=exits.append)
        return asyncio.get_running_loop().time() - began

    took = asyncio.run(_run())
    assert exits == [1] and took < 1


def test_the_first_signal_starts_the_shutdown_and_a_second_exits_at_once() -> None:
    exits: list[int] = []

    async def _run() -> bool:
        stop = asyncio.Event()
        handler = serve.stop_signal_handler(stop, exit_now=exits.append)
        handler()
        started, exits_after_first = stop.is_set(), list(exits)
        handler()
        return started and exits_after_first == []

    assert asyncio.run(_run())
    assert exits == [1]


def test_injected_runner_startup_failure_stops_owners_before_runner_and_database(monkeypatch, tmp_path) -> None:
    from looper import db

    stopped: list[str] = []

    async def stop(label: str) -> None:
        stopped.append(label)

    async def partial_start(*, ownership, **kwargs) -> None:
        ownership.push_async_callback(stop, "agents")
        ownership.push_async_callback(stop, "fleet")
        raise serve.StartupCheckFailed("startup failed")

    monkeypatch.setattr(serve, "_run_agent_system_owned", partial_start)
    monkeypatch.setattr(db, "close_pool", lambda: stop("database"))
    runner = SimpleNamespace(stop=lambda: stop("runner"))
    with pytest.raises(serve.StartupCheckFailed, match="startup failed"):
        asyncio.run(serve._run_agent_system(ops=None, runtime_root=tmp_path, work_dir=tmp_path,
                                           runner_supervisor=runner))
    assert stopped == ["fleet", "agents", "runner", "database"]


def test_lost_lock_fences_before_ending_owned_agents(monkeypatch, caplog):
    from looper.agents import proc_tree
    from looper.db import connection

    async def run():
        events = []
        monkeypatch.setattr(proc_tree, "fence_spawns", lambda: events.append("spawns fenced"))
        monkeypatch.setattr(connection, "fence", lambda: events.append("fenced"))
        async def end_owned(*, deadline):
            assert deadline == 10
            events.append("agents ended")
        monkeypatch.setattr(proc_tree, "end_owned", end_owned)
        fence = serve.Fence(exit_now=lambda code: events.append(code))
        fence.http_server = SimpleNamespace(close_listener=lambda: events.append("listener closed"))
        fence.worker_pool = SimpleNamespace(fence_kept=lambda: events.append("kept runs fenced"))
        fence.lost("connection lost during shutdown")
        assert events == ["spawns fenced", "fenced", "kept runs fenced", "listener closed"]
        assert fence._backstop is not None
        fence.exit(1)  # A normal shutdown deadline that fires after loss still exits 75.
        fence.lost("second notification")
        await fence._ending
        fence._backstop.cancel()
        assert events == ["spawns fenced", "fenced", "kept runs fenced", "listener closed", 75, "agents ended", 75]
    asyncio.run(run())
    assert "fenced the database pool" in caplog.text
    assert "closed the HTTP listener" in caplog.text
    assert "exiting with 75" in caplog.text


def test_lost_lock_before_http_still_exits_if_agent_cleanup_fails(monkeypatch):
    from looper.agents import proc_tree
    from looper.db import connection

    async def run():
        exits = []
        monkeypatch.setattr(proc_tree, "_spawns_fenced", False)
        monkeypatch.setattr(connection, "fence", lambda: None)
        async def end_owned(*, deadline):
            raise OSError("cannot read processes")
        monkeypatch.setattr(proc_tree, "end_owned", end_owned)
        fence = serve.Fence(exit_now=exits.append)
        fence.lost("lost during startup")
        with pytest.raises(OSError):
            await fence._ending
        fence._backstop.cancel()
        assert exits == [75]
    asyncio.run(run())


def test_startup_failure_closes_the_pool_before_releasing_the_lease(monkeypatch, tmp_path):
    from unittest.mock import AsyncMock
    from looper import db
    from looper.ops_config import KeeperMode, OpsConfig
    from looper.agents import operator_notes

    async def run():
        events = []
        async def closed():
            events.append("pool closed")
        async def released():
            events.append("lease released")
        lease = SimpleNamespace(release=released)
        monkeypatch.setattr(serve.keeper, "SOCKET_PATH_MAX", 1000)
        monkeypatch.setattr(operator_notes, "_root", str(tmp_path))
        monkeypatch.setattr(serve.self_check, "check_database", AsyncMock(return_value=SimpleNamespace(status="ok")))
        monkeypatch.setattr(serve.ControlPlaneLease, "acquire", AsyncMock(return_value=lease))
        monkeypatch.setattr(db, "init_pool", AsyncMock(return_value=object()))
        monkeypatch.setattr(db, "close_pool", closed)
        monkeypatch.setattr(serve, "_back_up_before_schema_change", AsyncMock(side_effect=OSError("startup failed")))
        ops = OpsConfig(runtime_root=tmp_path, path=tmp_path / "ops.toml", keeper_mode=KeeperMode.ON)
        with pytest.raises(OSError, match="startup failed"):
            await serve._run_agent_system_owned(ops=ops, runtime_root=tmp_path, work_dir=tmp_path)
        assert events == ["pool closed", "lease released"]
    asyncio.run(run())


def test_a_failed_database_check_runs_before_lock_acquisition(monkeypatch, tmp_path):
    from unittest.mock import AsyncMock
    from looper.ops_config import KeeperMode, OpsConfig
    from looper.agents import operator_notes

    async def run():
        monkeypatch.setattr(operator_notes, "_root", str(tmp_path))
        check = serve.self_check.Check("database", "Database", "fail", "unavailable")
        monkeypatch.setattr(serve.self_check, "check_database", AsyncMock(return_value=check))
        acquire = AsyncMock()
        monkeypatch.setattr(serve.ControlPlaneLease, "acquire", acquire)
        ops = OpsConfig(runtime_root=tmp_path, path=tmp_path / "ops.toml", keeper_mode=KeeperMode.ON)
        with pytest.raises(serve.StartupCheckFailed, match="unavailable"):
            await serve._run_agent_system_owned(ops=ops, runtime_root=tmp_path, work_dir=tmp_path)
        acquire.assert_not_awaited()
    asyncio.run(run())


@pytest.mark.parametrize("mode", ["on", "off"])
def test_keeper_socket_length_is_checked_before_pool_creation(monkeypatch, tmp_path, mode):
    from unittest.mock import AsyncMock
    from looper import db
    from looper.ops_config import KeeperMode, OpsConfig
    from looper.agents import operator_notes

    async def run():
        root = tmp_path / ("é" * 40)
        lease = SimpleNamespace(release=AsyncMock())
        monkeypatch.setattr(operator_notes, "_root", str(tmp_path))
        monkeypatch.setattr(serve.self_check, "check_database", AsyncMock(return_value=SimpleNamespace(status="ok")))
        monkeypatch.setattr(serve.ControlPlaneLease, "acquire", AsyncMock(return_value=lease))
        init_pool = AsyncMock(side_effect=OSError("pool creation reached"))
        monkeypatch.setattr(db, "init_pool", init_pool)
        monkeypatch.setattr(db, "close_pool", AsyncMock())
        ops = OpsConfig(runtime_root=root, path=root / "ops.toml", keeper_mode=KeeperMode(mode))
        if mode == "on":
            n = len(str(root / serve.keeper.RUNS_DIR / ("reviewer-" + "0" * 12) / serve.keeper.SOCKET).encode())
            with pytest.raises(serve.StartupCheckFailed, match=rf"keeper sockets \({n} > 103 bytes\)"):
                await serve._run_agent_system_owned(ops=ops, runtime_root=root, work_dir=root)
            init_pool.assert_not_awaited()
            lease.release.assert_awaited_once()
        else:
            with pytest.raises(OSError, match="pool creation reached"):
                await serve._run_agent_system_owned(ops=ops, runtime_root=root, work_dir=root)
            init_pool.assert_awaited_once()
    asyncio.run(run())
