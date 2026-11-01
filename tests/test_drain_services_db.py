"""Scheduler drain admission and completion on disposable PostgreSQL."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import pytest

from looper.agents.drain import DrainScope, DrainState
from looper.agents.scheduler import START_RETRY_AFTER, Scheduler
from looper.db import connection as db_connection, schedules as db_schedules, tasks as db_tasks
from tests.db_support import DSN
from tests.schedule_support import ScheduleEnv, run_scheduled

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")


def test_due_schedule_stays_due_until_restart_catches_up(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    async def test(env: ScheduleEnv) -> None:
        drain = DrainState()
        env.scheduler = Scheduler(bus=env.bus, clock=env.clock, drain=drain)
        sid = (await env.enabled(gate_command="echo work"))["schedule_id"]
        await env.due(sid, fires_ago=2)
        due = await env.pool.fetchval("SELECT next_fire_at FROM schedules WHERE id = $1", sid)
        drain.begin(DrainScope.RUNS)
        await env.scheduler.tick()
        assert await env.scheduler.run_now(sid) is None
        assert await env.runs(sid) == []
        assert await env.pool.fetchval("SELECT next_fire_at FROM schedules WHERE id = $1", sid) == due
        assert env.scheduler.drain_waiting() == {"gates": 0, "starting": 0}

        # A new process has a fresh serving state, but keeps the schedule's original due time.
        env.scheduler = Scheduler(bus=env.bus, clock=env.clock, drain=DrainState())
        try:
            await env.scheduler.tick()
            await env.scheduler.idle()
            run, = await env.runs(sid)
            assert (run["trigger"], run["status"], run["missed"]) == ("catch_up", "running", 3)
            assert len(env.bus.of("task_start")) == 1
            assert await env.pool.fetchval("SELECT count(*) FROM schedule_runs WHERE status = 'gate_failed'") == 0
        finally:
            await env.scheduler.stop()

    run_scheduled(test, monkeypatch, tmp_path)


def test_gate_already_admitted_finishes_during_drain(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from looper.agents import scheduler as module
    from looper.agents.schedule_gate import GateResult

    async def test(env: ScheduleEnv) -> None:
        entered, release = asyncio.Event(), asyncio.Event()

        async def gate(*args: Any, **kwargs: Any) -> GateResult:
            entered.set()
            await release.wait()
            return GateResult(0, "work", "")

        monkeypatch.setattr(module, "run_gate", gate)
        drain = DrainState()
        env.scheduler = Scheduler(bus=env.bus, clock=env.clock, drain=drain)
        drain.add_source(env.scheduler.drain_waiting)
        sid = (await env.enabled(gate_command="echo work"))["schedule_id"]
        await env.due(sid)
        try:
            await env.scheduler.tick()
            # Count the background gate even before its coroutine has reached its first await.
            assert env.scheduler.drain_waiting()["gates"] == 1
            await asyncio.wait_for(entered.wait(), 5)
            assert drain.begin(DrainScope.RUNS)["phase"] == "draining"
            release.set()
            await env.scheduler.idle()
            assert (await env.runs(sid))[0]["status"] == "running"
            assert len(env.bus.of("task_start")) == 1
            assert drain.snapshot()["phase"] == "drained"
        finally:
            release.set()
            await env.scheduler.stop()

    run_scheduled(test, monkeypatch, tmp_path)


def test_drain_during_schedule_lookup_preserves_the_due_fire(monkeypatch: pytest.MonkeyPatch,
                                                           tmp_path: Path) -> None:
    from looper.db import schedules

    async def test(env: ScheduleEnv) -> None:
        entered, release = asyncio.Event(), asyncio.Event()
        lock = schedules.lock_next_due

        async def lookup(*args: Any, **kwargs: Any) -> Any:
            row = await lock(*args, **kwargs)
            entered.set()
            await release.wait()
            return row

        drain = DrainState()
        env.scheduler = Scheduler(bus=env.bus, clock=env.clock, drain=drain)
        drain.add_source(env.scheduler.drain_waiting)
        sid = (await env.enabled())["schedule_id"]
        await env.due(sid)
        due = await env.pool.fetchval("SELECT next_fire_at FROM schedules WHERE id = $1", sid)
        monkeypatch.setattr(schedules, "lock_next_due", lookup)
        job = asyncio.create_task(env.scheduler.tick())
        try:
            await asyncio.wait_for(entered.wait(), 5)
            assert drain.begin(DrainScope.RUNS)["waiting"]["starting"] == 1
            release.set()
            await asyncio.wait_for(job, 5)
            assert await env.runs(sid) == []
            assert await env.pool.fetchval("SELECT next_fire_at FROM schedules WHERE id = $1", sid) == due
            assert drain.snapshot()["phase"] == "drained"
        finally:
            release.set()
            await asyncio.gather(job, return_exceptions=True)

    run_scheduled(test, monkeypatch, tmp_path)


@pytest.mark.parametrize("entry", ["due", "manual", "queued"])
def test_drain_during_an_agent_firing_rolls_it_back(monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
                                                  entry: str) -> None:
    async def test(env: ScheduleEnv) -> None:
        drain = DrainState()
        env.scheduler = Scheduler(bus=env.bus, clock=env.clock, drain=drain)
        drain.add_source(env.scheduler.drain_waiting)
        sid = (await env.enabled())["schedule_id"]
        if entry == "due":
            await env.due(sid)
        elif entry == "queued":
            await db_schedules.insert_run(env.pool, schedule_id=sid, scheduled_for=env.clock.now,
                                          trigger="manual", status=db_schedules.QUEUED)
        due = await env.pool.fetchval("SELECT next_fire_at FROM schedules WHERE id = $1", sid)
        entered, release = asyncio.Event(), asyncio.Event()
        open_runs = db_schedules.open_runs

        async def waiting_runs(*args: Any, **kwargs: Any) -> Any:
            if not entered.is_set():
                entered.set()
                await release.wait()
            return await open_runs(*args, **kwargs)

        monkeypatch.setattr(db_schedules, "open_runs", waiting_runs)
        job = asyncio.create_task(env.scheduler.run_now(sid) if entry == "manual" else env.scheduler.tick())
        try:
            await asyncio.wait_for(entered.wait(), 5)
            assert drain.begin(DrainScope.RUNS)["waiting"]["starting"] == 1
            release.set()
            result = await asyncio.wait_for(job, 5)
            assert await env.pool.fetchval("SELECT count(*) FROM tasks WHERE schedule_id = $1", sid) == 0
            assert env.bus.of("task_start") == []
            if entry == "queued":
                run, = await env.runs(sid)
                assert run["status"] == db_schedules.QUEUED
            else:
                assert await env.runs(sid) == []
                if entry == "manual":
                    assert result is None
            assert await env.pool.fetchval("SELECT next_fire_at FROM schedules WHERE id = $1", sid) == due
            assert drain.snapshot()["phase"] == "drained"

            # Restart with the same database and a new process-local admission state.
            env.scheduler = Scheduler(bus=env.bus, clock=env.clock, drain=DrainState())
            await env.scheduler.tick()
            if entry in {"due", "queued"}:
                run, = await env.runs(sid)
                assert run["status"] == db_schedules.RUNNING
                assert env.bus.of("task_start") == [run["task_id"]]
            else:
                assert await env.runs(sid) == []
                assert env.bus.of("task_start") == []
        finally:
            release.set()
            await asyncio.gather(job, return_exceptions=True)

    run_scheduled(test, monkeypatch, tmp_path)


def test_drain_as_an_agent_firing_commits_defers_its_task_start(monkeypatch: pytest.MonkeyPatch,
                                                             tmp_path: Path) -> None:
    async def test(env: ScheduleEnv) -> None:
        drain = DrainState()
        env.scheduler = Scheduler(bus=env.bus, clock=env.clock, drain=drain)
        drain.add_source(env.scheduler.drain_waiting)
        sid = (await env.schedule())["schedule_id"]
        transaction = db_connection.transaction
        armed = False

        @asynccontextmanager
        async def draining_commit() -> AsyncIterator[Any]:
            nonlocal armed
            async with transaction() as conn:
                yield conn
            if armed:
                armed = False
                assert drain.begin(DrainScope.RUNS)["waiting"]["starting"] == 1

        monkeypatch.setattr(db_connection, "transaction", draining_commit)
        armed = True
        run_id = await env.scheduler.run_now(sid)
        run, = await env.runs(sid)
        assert run_id == run["run_id"]
        assert run["status"] == db_schedules.RUNNING
        assert await env.pool.fetchval("SELECT status FROM tasks WHERE id = $1", run["task_id"]) == db_tasks.READY
        assert env.bus.of("task_start") == []
        assert drain.snapshot()["phase"] == "drained"

        env.clock.now += START_RETRY_AFTER
        await env.scheduler.tick()
        assert env.bus.of("task_start") == []
        drain.cancel()
        await env.scheduler.tick()
        assert env.bus.of("task_start") == [run["task_id"]]

    run_scheduled(test, monkeypatch, tmp_path)
