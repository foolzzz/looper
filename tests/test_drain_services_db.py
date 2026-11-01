"""Scheduler drain admission and completion on disposable PostgreSQL."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from looper.agents.drain import DrainScope, DrainState
from looper.agents.scheduler import START_RETRY_AFTER, Scheduler
from looper.db import connection as db_connection, schedules as db_schedules, tasks as db_tasks
from tests.db_support import DSN, until
from tests.schedule_support import ScheduleEnv, run_scheduled

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")


@pytest.mark.parametrize("task_started", [False, True])
def test_a_gate_counts_toward_the_run_timeout_without_a_drain(monkeypatch: pytest.MonkeyPatch,
                                                            tmp_path: Path, task_started: bool) -> None:
    async def test(env: ScheduleEnv) -> None:
        sid = (await env.schedule(gate_command="sleep 2; echo work", run_timeout_seconds=1))["schedule_id"]
        fired_at = env.clock.now
        run_id = await env.scheduler.run_now(sid)
        # Advance the scheduler's clock with the real gate, before TASK_START is published.
        env.clock.now += timedelta(seconds=2)
        await asyncio.wait_for(env.scheduler.idle(), 5)
        run = await db_schedules.get_run(run_id)
        assert run["status"] == db_schedules.RUNNING
        assert run["fired_at"] == fired_at and run["started_at"] is None
        assert run["deferred_by_drain"] is False
        assert env.bus.of("task_start") == [run["task_id"]]
        if task_started:
            await db_tasks.start_ready_task(run["task_id"])

        await env.scheduler.tick()
        assert (await db_schedules.get_run(run_id))["status"] == "timed_out"
        assert env.bus.of("task_stop") == [run["task_id"]]
        assert (await env.runs(sid))[0]["duration_seconds"] == 2
        stats = await db_schedules.run_stats(sid, env.clock.now)
        for window in ("day", "week"):
            assert stats[window]["avg_seconds"] == stats[window]["max_seconds"] == 2

    run_scheduled(test, monkeypatch, tmp_path)


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


@pytest.mark.parametrize("entry", ["due", "manual", "queued"])
@pytest.mark.parametrize("recovery", ["cancel", "restart"])
def test_drain_as_a_gated_firing_commits_defers_its_gate(monkeypatch: pytest.MonkeyPatch,
                                                       tmp_path: Path, entry: str, recovery: str) -> None:
    async def test(env: ScheduleEnv) -> None:
        drain = DrainState()
        env.scheduler = Scheduler(bus=env.bus, clock=env.clock, drain=drain)
        drain.add_source(env.scheduler.drain_waiting)
        sid = (await env.enabled(gate_command="sleep 2; printf work >> gate-started; printf work",
                                 run_timeout_seconds=1))["schedule_id"]
        queued_id = None
        if entry == "due":
            await env.due(sid)
        elif entry == "queued":
            queued_id = await db_schedules.insert_run(env.pool, schedule_id=sid, scheduled_for=env.clock.now,
                                                      trigger="manual", status=db_schedules.QUEUED)
        transaction = db_connection.transaction
        armed = True

        @asynccontextmanager
        async def draining_commit() -> AsyncIterator[Any]:
            nonlocal armed
            async with transaction() as conn:
                yield conn
            if armed:
                armed = False
                assert drain.begin(DrainScope.RUNS)["waiting"]["starting"] == 1

        with monkeypatch.context() as patch:
            patch.setattr(db_connection, "transaction", draining_commit)
            if entry == "manual":
                await env.scheduler.run_now(sid)
            else:
                await env.scheduler.tick()
        await asyncio.wait_for(env.scheduler.idle(), 5)
        run, = await env.runs(sid)
        assert run["status"] == db_schedules.QUEUED
        if queued_id is not None:
            assert run["run_id"] == queued_id
        assert (await db_schedules.get_run(run["run_id"]))["deferred_by_drain"] is True
        assert run["task_id"] is None
        marker = tmp_path / "gate-started"
        assert not marker.exists()
        assert await env.pool.fetchval("SELECT count(*) FROM tasks WHERE schedule_id = $1", sid) == 0
        assert env.bus.of("task_start") == []
        assert env.scheduler.drain_waiting() == {"gates": 0, "starting": 0}
        assert drain.snapshot()["phase"] == "drained"

        await env.scheduler.tick()
        assert (await db_schedules.get_run(run["run_id"]))["status"] == db_schedules.QUEUED
        assert not marker.exists()
        if entry == "queued":
            await db_schedules.update_run(env.pool, run["run_id"], started_at=env.clock.now)
        env.clock.now += timedelta(hours=1)
        await env.pool.execute("UPDATE schedules SET next_fire_at = $2 WHERE id = $1",
                               sid, env.clock.now + timedelta(hours=1))
        fired_at = env.clock.now
        if recovery == "cancel":
            drain.cancel()
            await env.scheduler.tick()
        else:
            await env.scheduler.stop()
            env.scheduler = Scheduler(bus=env.bus, clock=env.clock, drain=DrainState())
            await env.scheduler.start()
            await until(lambda: env.scheduler.drain_waiting()["gates"], timeout=5)
        env.clock.now += timedelta(seconds=2)
        await until(lambda: env.bus.of("task_start"), timeout=5)
        await asyncio.wait_for(env.scheduler.idle(), 5)
        started, = await env.runs(sid)
        assert started["run_id"] == run["run_id"]
        assert started["status"] == db_schedules.RUNNING
        assert started["gate_output"] == "work"
        row = await db_schedules.get_run(started["run_id"])
        assert row["deferred_by_drain"] is False and row["started_at"] is None
        assert row["fired_at"] == fired_at
        assert marker.read_text() == "work"
        assert env.bus.of("task_start") == [started["task_id"]]
        assert await env.pool.fetchval("SELECT consecutive_failures FROM schedules WHERE id = $1", sid) == 0
        await env.scheduler.tick()
        assert (await db_schedules.get_run(row["id"]))["status"] == "timed_out"
        assert env.bus.of("task_stop") == [started["task_id"]]

    run_scheduled(test, monkeypatch, tmp_path)


@pytest.mark.parametrize("recovery", ["cancel", "restart"])
def test_parallel_deferred_gate_resumes_before_the_next_due_fire(monkeypatch: pytest.MonkeyPatch,
                                                                tmp_path: Path, recovery: str) -> None:
    from looper.agents import scheduler as module
    from looper.agents.schedule_gate import GateResult

    async def test(env: ScheduleEnv) -> None:
        release = asyncio.Event()
        release.set()
        checked = []

        async def gate(*args: Any, **kwargs: Any) -> GateResult:
            checked.append(kwargs["env"]["LOOPER_SCHEDULE_RUN_ID"])
            await release.wait()
            return GateResult(0, "work", "")

        monkeypatch.setattr(module, "run_gate", gate)
        drain = DrainState()
        env.scheduler = Scheduler(bus=env.bus, clock=env.clock, drain=drain)
        sid = (await env.enabled(cron="* * * * *", gate_command="echo work", overlap="parallel",
                                 max_parallel_runs=2))["schedule_id"]
        first_id = await env.scheduler.run_now(sid)
        await asyncio.wait_for(env.scheduler.idle(), 5)
        first = await db_schedules.get_run(first_id)
        assert await db_tasks.start_ready_task(first["task_id"]) is not None
        await env.pool.execute("UPDATE tasks SET status = 'running' WHERE id = $1", first["task_id"])
        transaction = db_connection.transaction

        @asynccontextmanager
        async def draining_commit() -> AsyncIterator[Any]:
            async with transaction() as conn:
                yield conn
            drain.begin(DrainScope.RUNS)

        with monkeypatch.context() as patch:
            patch.setattr(db_connection, "transaction", draining_commit)
            deferred_id = await env.scheduler.run_now(sid)
        deferred = await db_schedules.get_run(deferred_id)
        assert deferred["status"] == db_schedules.QUEUED and deferred["deferred_by_drain"]
        assert checked == [first_id]
        await env.scheduler.tick()
        assert (await db_schedules.get_run(deferred_id))["status"] == db_schedules.QUEUED

        # The next minute is due while the earlier task still runs. Hold the resumed gate to
        # observe which firing gets the free slot, without depending on coroutine timing.
        env.clock.now = (env.clock.now + timedelta(minutes=1)).replace(second=10, microsecond=0)
        await env.pool.execute("UPDATE schedules SET next_fire_at = $2 WHERE id = $1", sid,
                               env.clock.now.replace(second=0))
        release.clear()
        if recovery == "cancel":
            drain.cancel()
            await env.scheduler.tick()
        else:
            await env.scheduler.stop()
            env.scheduler = Scheduler(bus=env.bus, clock=env.clock, drain=DrainState())
            await env.scheduler.start()
        try:
            await until(lambda: len(checked) == 2, timeout=5)
            assert checked == [first_id, deferred_id]
            resumed = await db_schedules.get_run(deferred_id)
            assert resumed["status"] == db_schedules.GATING and not resumed["deferred_by_drain"]
            # Finish the background tick before inspecting the newer firing.
            if recovery == "restart":
                await until(lambda: env.scheduler.health()["last_ok_tick_at"] is not None, timeout=5)
            runs = await env.runs(sid)
            assert len(runs) == 3
            newer = [run for run in runs if run["run_id"] not in {first_id, deferred_id}]
            assert [run["status"] for run in newer] == ["skipped_overlap"]
            release.set()
            await asyncio.wait_for(env.scheduler.idle(), 5)
            resumed = await db_schedules.get_run(deferred_id)
            assert resumed["status"] == db_schedules.RUNNING and resumed["gate_output"] == "work"
            assert (await db_schedules.get_run(first_id))["task_status"] == "running"
            assert env.bus.of("task_start") == [first["task_id"], resumed["task_id"]]
            await env.scheduler.tick()
            assert checked == [first_id, deferred_id]
        finally:
            release.set()
            await env.scheduler.stop()

    run_scheduled(test, monkeypatch, tmp_path)


@pytest.mark.parametrize("blocker", ["capacity", "gate"])
def test_parallel_deferred_gate_waits_for_admission(monkeypatch: pytest.MonkeyPatch,
                                                   tmp_path: Path, blocker: str) -> None:
    async def test(env: ScheduleEnv) -> None:
        sid = (await env.schedule(gate_command="printf work", overlap="parallel",
                                  max_parallel_runs=2))["schedule_id"]
        first_id = await env.scheduler.run_now(sid)
        await asyncio.wait_for(env.scheduler.idle(), 5)
        first = await db_schedules.get_run(first_id)
        assert await db_tasks.start_ready_task(first["task_id"]) is not None
        await env.pool.execute("UPDATE tasks SET status = 'running' WHERE id = $1", first["task_id"])
        deferred_id = await db_schedules.insert_run(env.pool, schedule_id=sid, scheduled_for=env.clock.now,
                                                    trigger="manual", status=db_schedules.QUEUED)
        await env.pool.execute("UPDATE schedule_runs SET deferred_by_drain = true WHERE id = $1", deferred_id)
        if blocker == "capacity":
            await db_schedules.update_schedule(env.pool, sid, {"max_parallel_runs": 1})
        else:
            gating_id = await db_schedules.insert_run(env.pool, schedule_id=sid, scheduled_for=env.clock.now,
                                                      trigger="manual", status=db_schedules.GATING)

        await env.scheduler.tick()
        await asyncio.wait_for(env.scheduler.idle(), 5)
        waiting = await db_schedules.get_run(deferred_id)
        assert waiting["status"] == db_schedules.QUEUED and waiting["deferred_by_drain"]
        assert env.bus.of("task_start") == [first["task_id"]]
        if blocker == "capacity":
            await db_schedules.update_schedule(env.pool, sid, {"max_parallel_runs": 2})
        else:
            await db_schedules.update_run(env.pool, gating_id, status="skipped_gate", finished_at=env.clock.now)
        await env.scheduler.tick()
        await asyncio.wait_for(env.scheduler.idle(), 5)
        resumed = await db_schedules.get_run(deferred_id)
        assert resumed["status"] == db_schedules.RUNNING and not resumed["deferred_by_drain"]
        assert resumed["gate_output"] == "work"
        assert env.bus.of("task_start") == [first["task_id"], resumed["task_id"]]

    run_scheduled(test, monkeypatch, tmp_path)


@pytest.mark.parametrize("entry", ["manual", "queued"])
@pytest.mark.parametrize("start_via", ["scheduler", "task_api", "task_api_concurrent_anchor"])
def test_drain_as_an_agent_firing_commits_defers_its_task_start(monkeypatch: pytest.MonkeyPatch,
                                                             tmp_path: Path, caplog: pytest.LogCaptureFixture,
                                                             entry: str, start_via: str) -> None:
    async def test(env: ScheduleEnv) -> None:
        drain = DrainState()
        env.scheduler = Scheduler(bus=env.bus, clock=env.clock, drain=drain)
        drain.add_source(env.scheduler.drain_waiting)
        sid = (await env.schedule(run_timeout_seconds=60))["schedule_id"]
        if entry == "queued":
            await db_schedules.insert_run(env.pool, schedule_id=sid, scheduled_for=env.clock.now,
                                          trigger="manual", status=db_schedules.QUEUED)
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
        with caplog.at_level("INFO", logger="looper.agents.scheduler"):
            if entry == "manual":
                run_id = await env.scheduler.run_now(sid)
            else:
                await env.scheduler.tick()
                run_id = (await env.runs(sid))[0]["run_id"]
        run, = await env.runs(sid)
        assert run_id == run["run_id"]
        assert run["status"] == db_schedules.RUNNING
        assert await env.pool.fetchval("SELECT status FROM tasks WHERE id = $1", run["task_id"]) == db_tasks.READY
        assert env.bus.of("task_start") == []
        assert drain.snapshot()["phase"] == "drained"
        assert "starts now" not in caplog.text
        assert "defer" in caplog.text

        env.clock.now += timedelta(minutes=5)
        await env.scheduler.tick()
        waiting = await db_schedules.get_run(run_id)
        assert waiting["status"] == db_schedules.RUNNING
        assert waiting["run_timeout_seconds"] == 60 and waiting["started_at"] is None
        assert waiting["deferred_by_drain"] is True
        assert await env.pool.fetchval("SELECT status FROM tasks WHERE id = $1", run["task_id"]) == db_tasks.READY
        assert await env.pool.fetchval("SELECT consecutive_failures FROM schedules WHERE id = $1", sid) == 0
        assert env.bus.of("task_stop") == []
        assert env.bus.of("task_start") == []
        drain.cancel()
        # Recovery also keeps the deferred timeout decision across a scheduler restart.
        env.scheduler = Scheduler(bus=env.bus, clock=env.clock, drain=DrainState())
        if start_via != "scheduler":
            from types import SimpleNamespace

            from looper.agents.http_server import AgentHTTPServer
            from tests.db_support import call_v1

            server = AgentHTTPServer(web_adapter=None, task_router=None,
                                     supervisor=SimpleNamespace(bus=env.bus), runtime_root=env.runtime_root)
            status, answer = await call_v1(server, "/api/v1/ops/task_control/start", {"task_id": run["task_id"]})
            assert status == 200 and answer["started"] is True
            # The TaskRouter claims the API's TASK_START, without the scheduler publishing it.
            assert await db_tasks.start_ready_task(run["task_id"]) is not None
            assert (await db_schedules.get_run(run_id))["started_at"] is None
        started_at = env.clock.now
        with monkeypatch.context() as patch:
            if start_via == "task_api_concurrent_anchor":
                started_at -= timedelta(seconds=10)
                running_runs = db_schedules.running_runs

                async def concurrently_anchored_runs() -> list[dict[str, Any]]:
                    rows = await running_runs()
                    # Another start writes the anchor after the tick reads its snapshot.
                    await db_schedules.update_run(env.pool, run_id, started_at=started_at)
                    return rows

                patch.setattr(db_schedules, "running_runs", concurrently_anchored_runs)
            await env.scheduler.tick()
        assert env.bus.of("task_start") == [run["task_id"]]
        started = await db_schedules.get_run(run_id)
        assert started["started_at"] == started_at and started["fired_at"] == waiting["fired_at"]
        assert started["deferred_by_drain"] is True
        # The TaskRouter claims TASK_START; time spent draining is outside the run's timeout.
        if start_via == "scheduler":
            await db_tasks.start_ready_task(run["task_id"])
        env.clock.now = started_at + timedelta(seconds=59)
        await env.scheduler.tick()
        running = await db_schedules.get_run(run_id)
        assert running["status"] == db_schedules.RUNNING
        assert running["started_at"] == started["started_at"]
        assert env.bus.of("task_stop") == []
        env.clock.now += timedelta(seconds=1)
        await env.scheduler.tick()
        assert (await db_schedules.get_run(run_id))["status"] == "timed_out"
        assert await env.pool.fetchval("SELECT consecutive_failures FROM schedules WHERE id = $1", sid) == 1
        assert env.bus.of("task_start") == env.bus.of("task_stop") == [run["task_id"]]

    run_scheduled(test, monkeypatch, tmp_path)


@pytest.mark.parametrize("entry", ["manual", "queued"])
def test_drain_during_task_lookup_defers_its_start(monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
                                                 caplog: pytest.LogCaptureFixture, entry: str) -> None:
    async def test(env: ScheduleEnv) -> None:
        drain = DrainState()
        env.scheduler = Scheduler(bus=env.bus, clock=env.clock, drain=drain)
        drain.add_source(env.scheduler.drain_waiting)
        sid = (await env.schedule())["schedule_id"]
        if entry == "queued":
            await db_schedules.insert_run(env.pool, schedule_id=sid, scheduled_for=env.clock.now,
                                          trigger="manual", status=db_schedules.QUEUED)
        get_task = db_tasks.get_task

        async def draining_lookup(*args: Any, **kwargs: Any) -> Any:
            task = await get_task(*args, **kwargs)
            drain.begin(DrainScope.RUNS)
            return task

        with monkeypatch.context() as patch, caplog.at_level("INFO", logger="looper.agents.scheduler"):
            patch.setattr(db_tasks, "get_task", draining_lookup)
            if entry == "manual":
                await env.scheduler.run_now(sid)
            else:
                await env.scheduler.tick()
        run, = await env.runs(sid)
        assert run["status"] == db_schedules.RUNNING
        assert await env.pool.fetchval("SELECT status FROM tasks WHERE id = $1", run["task_id"]) == db_tasks.READY
        assert env.bus.of("task_start") == []
        assert drain.snapshot()["phase"] == "drained"
        assert "starts now" not in caplog.text
        assert "defer" in caplog.text

        assert (await db_schedules.get_run(run["run_id"]))["deferred_by_drain"] is True
        env.clock.now += START_RETRY_AFTER
        await env.scheduler.tick()
        assert env.bus.of("task_start") == []
        drain.cancel()
        await env.scheduler.tick()
        assert env.bus.of("task_start") == [run["task_id"]]
        await db_tasks.start_ready_task(run["task_id"])
        await env.scheduler.tick()
        assert env.bus.of("task_start") == [run["task_id"]]

    run_scheduled(test, monkeypatch, tmp_path)
