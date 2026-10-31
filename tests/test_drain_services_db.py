"""Scheduler drain admission and completion on disposable PostgreSQL."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from looper.agents.drain import DrainScope, DrainState
from looper.agents.scheduler import Scheduler
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
