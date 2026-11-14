"""Control-plane lease health and terminal ownership transitions."""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, Mock

import pytest

from looper.agents import control_plane as control


def test_holder_identifies_the_database_backend_pid():
    holder = control.LockHolder(123, "looper serve 456@host", "127.0.0.1", None)
    assert holder.describe().startswith("backend_pid=123, application_name=looper serve 456@host")


def test_hidden_holder_fields_are_unknown():
    assert control.LockHolder(None, None, None, None).describe().count("unknown") == 4


@pytest.mark.parametrize("failure", [False, OSError("silent connection failure"),
                                     control.asyncpg.ConnectionDoesNotExistError("connection lost")])
def test_the_health_check_catches_a_silent_loss(monkeypatch, failure):
    async def run():
        conn = Mock(is_closed=Mock(return_value=False))
        conn.fetchval = AsyncMock(return_value=True)
        conn.close = AsyncMock()
        monkeypatch.setattr(control.asyncpg, "connect", AsyncMock(return_value=conn))
        monkeypatch.setattr(control, "HEALTH_INTERVAL_SECONDS", 0.01)
        monkeypatch.setattr(control, "HEALTH_TIMEOUT_SECONDS", 0.02)
        reasons = []
        lease = await control.ControlPlaneLease.acquire("unused", on_lost=reasons.append)
        async def health(*args):
            if failure == "timeout":
                await asyncio.Event().wait()
            if isinstance(failure, Exception):
                raise failure
            return failure
        conn.fetchval.side_effect = health
        try:
            async with asyncio.timeout(0.5):
                while not reasons:
                    await asyncio.sleep(0.01)
            assert lease.state is control.LeaseState.LOST
            lease.lose("again")
            await lease.release()
            assert lease.state is control.LeaseState.LOST
            assert len(reasons) == 1
            with pytest.raises(RuntimeError, match="held"):
                await lease.bump_epoch()
            assert conn.fetchval.call_count == 2
        finally:
            await lease.release()
    asyncio.run(run())


def test_release_does_not_report_a_lost_lock(monkeypatch):
    async def run():
        conn = Mock()
        conn.fetchval = AsyncMock(return_value=True)
        reasons = []
        async def close():
            conn.add_termination_listener.call_args.args[0](conn)
        conn.close = close
        monkeypatch.setattr(control.asyncpg, "connect", AsyncMock(return_value=conn))
        lease = await control.ControlPlaneLease.acquire("unused", on_lost=reasons.append)
        await lease.release()
        lease.lose("late notification")
        assert lease.state is control.LeaseState.RELEASED
        assert reasons == []
    asyncio.run(run())


def test_an_acquisition_error_closes_the_connection(monkeypatch):
    async def run():
        conn = Mock(fetchval=AsyncMock(side_effect=OSError("query failed")), close=AsyncMock())
        monkeypatch.setattr(control.asyncpg, "connect", AsyncMock(return_value=conn))
        with pytest.raises(OSError):
            await control.ControlPlaneLease.acquire("unused", on_lost=Mock())
        conn.close.assert_awaited_once()
    asyncio.run(run())


def test_a_holder_that_released_during_inspection_is_unknown(monkeypatch):
    async def run():
        conn = Mock(fetchval=AsyncMock(return_value=False), fetchrow=AsyncMock(return_value=None), close=AsyncMock())
        monkeypatch.setattr(control.asyncpg, "connect", AsyncMock(return_value=conn))
        with pytest.raises(control.LockHeld, match="unknown") as caught:
            await control.ControlPlaneLease.acquire("unused", on_lost=Mock())
        assert caught.value.holder == control.LockHolder(None, None, None, None)
        conn.close.assert_awaited_once()
    asyncio.run(run())


def test_a_fence_during_pool_creation_cannot_publish_a_new_pool(monkeypatch):
    from looper.db import connection

    async def run():
        pool = Mock()
        async def create_pool(*args, **kwargs):
            connection.fence()
            return pool
        monkeypatch.setattr(connection, "_fenced", False)
        monkeypatch.setattr(connection, "_pool", None)
        monkeypatch.setattr(connection.asyncpg, "create_pool", create_pool)
        with pytest.raises(RuntimeError, match="fenced"):
            await connection.init_pool("unused")
        pool.terminate.assert_called_once()
        with pytest.raises(RuntimeError, match="fenced"):
            connection.get_pool()
        with pytest.raises(RuntimeError, match="fenced"):
            await connection.init_pool("unused")
        assert connection._pool is None
    asyncio.run(run())


def test_releasing_a_half_open_connection_is_bounded(monkeypatch):
    async def run():
        conn = Mock(fetchval=AsyncMock(return_value=True))
        async def close():
            await asyncio.Event().wait()
        conn.close = close
        monkeypatch.setattr(control.asyncpg, "connect", AsyncMock(return_value=conn))
        monkeypatch.setattr(control, "HEALTH_TIMEOUT_SECONDS", 0.01)
        reasons = []
        lease = await control.ControlPlaneLease.acquire("unused", on_lost=reasons.append)
        await asyncio.wait_for(lease.release(), 0.5)
        conn.terminate.assert_called_once()
        assert lease.state is control.LeaseState.RELEASED
        assert reasons == []
    asyncio.run(run())


@pytest.mark.parametrize("recheck", [True, False, "timeout", "closed"])
def test_uncertain_lease_fences_spawns_until_rechecked(monkeypatch, recheck):
    from looper.agents import proc_tree

    async def run():
        monkeypatch.setattr(proc_tree, "_spawns_fenced", False)
        monkeypatch.setattr(proc_tree, "_lease_uncertain", False, raising=False)
        monkeypatch.setattr(control, "HEALTH_INTERVAL_SECONDS", 0.001)
        monkeypatch.setattr(control, "HEALTH_TIMEOUT_SECONDS", 0.01)
        monkeypatch.setattr(control, "RECHECK_TIMEOUT_SECONDS", 0.02, raising=False)
        monkeypatch.setattr(control, "RECHECK_WINDOW_SECONDS", 0.04, raising=False)
        conn = Mock(close=AsyncMock(), is_closed=Mock(return_value=False))
        reasons, calls = [], []
        recovered = asyncio.Event()

        async def query(*args):
            calls.append(asyncio.get_running_loop().time())
            if len(calls) == 1 or recheck == "timeout":
                await asyncio.sleep(6)
            assert lease.state is control.LeaseState.UNCERTAIN
            assert proc_tree.spawns_fenced()
            with pytest.raises(RuntimeError, match="uncertain"):
                await proc_tree.spawn("must-not-start")
            if recheck == "closed":
                raise control.asyncpg.ConnectionDoesNotExistError("disconnected")
            recovered.set()
            return recheck

        conn.fetchval = query
        lease = control.ControlPlaneLease(conn, reasons.append)
        lease._health_task = asyncio.create_task(lease._health())
        try:
            async with asyncio.timeout(0.5):
                if recheck is True:
                    await recovered.wait()
                    assert lease.state is control.LeaseState.HELD
                    assert not proc_tree.spawns_fenced()
                    assert not reasons
                else:
                    while not reasons:
                        await asyncio.sleep(0.001)
                    assert lease.state is control.LeaseState.LOST
                    if recheck == "timeout":
                        assert len(calls) == 3
                        assert calls[-1] - calls[1] >= 0.015
        finally:
            await lease.release()
    asyncio.run(run())


@pytest.mark.parametrize("result", [True, ValueError("delayed reply"), False,
                                    control.asyncpg.ConnectionDoesNotExistError("disconnected")])
def test_query_overlapping_stall_requires_a_clean_recheck(monkeypatch, result):
    from looper.agents import proc_tree

    async def run():
        monkeypatch.setattr(proc_tree, "_lease_uncertain", False)
        monkeypatch.setattr(proc_tree, "_spawns_fenced", False)
        monkeypatch.setattr(control, "HEALTH_INTERVAL_SECONDS", 0.001)
        watch = Mock(stall_seconds_between=Mock(side_effect=[6.0, 0.0]))
        reasons = []
        done = asyncio.Event()
        calls = 0

        async def query(*args):
            nonlocal calls
            calls += 1
            if calls == 1:
                if isinstance(result, Exception):
                    raise result
                return result
            assert lease.state is control.LeaseState.UNCERTAIN
            assert proc_tree.spawns_fenced()
            proc_tree.fence_spawns()  # Recovery must never undo a terminal fence.
            done.set()
            return True

        conn = Mock(fetchval=query, close=AsyncMock(), is_closed=Mock(return_value=False))
        lease = control.ControlPlaneLease(conn, reasons.append, loop_watch=watch)
        lease._health_task = asyncio.create_task(lease._health())
        try:
            async with asyncio.timeout(0.5):
                if result is False or isinstance(result, control.asyncpg.PostgresConnectionError):
                    while not reasons:
                        await asyncio.sleep(0.001)
                    assert calls == 1
                else:
                    await done.wait()
                    assert not reasons
                    assert lease.state is control.LeaseState.HELD
                    assert proc_tree.spawns_fenced()
        finally:
            await lease.release()
    asyncio.run(run())
