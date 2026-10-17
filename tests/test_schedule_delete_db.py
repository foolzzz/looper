"""Deleting a schedule while a run of it is in progress, on a real PostgreSQL: the delete succeeds, a task under way
keeps running and ends as any task does, and nothing errors, brings the schedule back or sends a message about it.

Runs only when LOOPER_TEST_DATABASE_URL points at a disposable database; it uses the schedule tests' helpers.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from tests.test_schedules_db import DSN, _Env, _run
from tests.test_schedules_s2_db import TELEGRAM_CHAT, _allow_telegram, _setup

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")


async def _gone(env: _Env, sid: str) -> None:
    """Nothing of the schedule is left, and nothing brought it back."""
    assert (await env.call("GET", f"/api/schedules/{sid}"))[0] == 404
    for table in ("schedules", "schedule_runs", "schedule_notifications"):
        assert await env.pool.fetchval(f"SELECT count(*) FROM {table}") == 0, table


def _errors(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [record.getMessage() for record in caplog.records if record.levelno >= logging.WARNING]


def test_a_schedule_deleted_while_its_task_runs_lets_the_task_end_and_says_nothing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture,
) -> None:
    async def test(env: _Env) -> None:
        sender = _setup(env)
        await _allow_telegram(env)
        sid = (await env.enabled(notify="always", notify_target={"channel": "telegram", "chat": TELEGRAM_CHAT},
                                 run_timeout_seconds=60))["schedule_id"]
        run = (await env.ok("POST", f"/api/schedules/{sid}/run"))["run"]
        task_id = run["task_id"]
        await env.pool.execute("UPDATE tasks SET status = 'running' WHERE id = $1", task_id)
        caplog.clear()

        assert await env.ok("DELETE", f"/api/schedules/{sid}") == {"deleted": sid}
        task = await env.ok("GET", f"/api/tasks/{task_id}")
        assert (task["status"], task["schedule_id"]) == ("running", None)
        await env.scheduler.tick()  # the run's timeout or its task's end are no longer the scheduler's
        env.clock.now += timedelta(seconds=120)
        await env.scheduler.tick()
        assert env.bus.of("task_stop") == []

        await env.end_task(task_id, "completed", summary="done", cost=0.25)
        await env.scheduler.tick()
        await env.scheduler.idle()
        assert (await env.ok("GET", f"/api/tasks/{task_id}"))["status"] == "completed"
        await _gone(env, sid)
        assert sender.sent == []
        assert _errors(caplog) == []

    caplog.set_level(logging.INFO)
    _run(test, monkeypatch, tmp_path)


def test_a_schedule_deleted_while_its_gate_runs_creates_no_task(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture,
) -> None:
    async def test(env: _Env) -> None:
        sender = _setup(env)
        gate = "touch started; while [ ! -e go ]; do sleep 0.05; done; echo alert-1"
        sid = (await env.enabled(gate_command=gate, gate_timeout_seconds=30, notify="always"))["schedule_id"]
        run = (await env.ok("POST", f"/api/schedules/{sid}/run"))["run"]
        assert run["status"] == "gating"
        for _ in range(500):  # the gate is under way
            if (tmp_path / "started").exists():
                break
            await asyncio.sleep(0.01)
        assert (tmp_path / "started").exists()
        caplog.clear()

        assert await env.ok("DELETE", f"/api/schedules/{sid}") == {"deleted": sid}
        (tmp_path / "go").write_text("")  # the gate finds something now
        await env.scheduler.idle()
        await env.scheduler.tick()
        assert env.bus.of("task_start") == []
        assert await env.pool.fetchval("SELECT count(*) FROM tasks") == 0
        await _gone(env, sid)
        assert sender.sent == []
        assert _errors(caplog) == []

    caplog.set_level(logging.INFO)
    _run(test, monkeypatch, tmp_path)


def test_a_schedule_deleted_with_a_queued_run_never_starts_it(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture,
) -> None:
    async def test(env: _Env) -> None:
        sid = (await env.enabled(overlap="queue"))["schedule_id"]
        first = (await env.ok("POST", f"/api/schedules/{sid}/run"))["run"]
        queued = (await env.ok("POST", f"/api/schedules/{sid}/run"))["run"]
        assert (first["status"], queued["status"]) == ("running", "queued")
        caplog.clear()

        assert await env.ok("DELETE", f"/api/schedules/{sid}") == {"deleted": sid}
        await env.end_task(first["task_id"], "completed")
        await env.scheduler.tick()
        assert env.bus.of("task_start") == [first["task_id"]]  # the queued run started nothing
        assert await env.pool.fetchval("SELECT count(*) FROM tasks") == 1
        await _gone(env, sid)
        assert _errors(caplog) == []

    caplog.set_level(logging.INFO)
    _run(test, monkeypatch, tmp_path)


async def _waits_for_a_lock(pool: Any, query: str) -> None:
    """Return once a backend running ``query`` waits for a row lock."""
    for _ in range(500):
        if await pool.fetchval(
                "SELECT count(*) FROM pg_stat_activity WHERE datname = current_database() AND wait_event_type = 'Lock' "
                "AND query LIKE $1", f"%{query}%"):
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"{query!r} never waited for a lock")


def test_a_schedule_deleted_while_its_runs_end_is_recorded_is_deleted_and_the_end_recorded_first(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture,
) -> None:
    """The run's end is being written (its row, then the schedule's counters) when the delete comes: the delete waits
    for it and succeeds, and the end is recorded without an error, never a deadlock that fails one of them."""
    from looper.db import schedules as db_schedules

    async def test(env: _Env) -> None:
        _setup(env)
        sid = (await env.enabled())["schedule_id"]
        task_id = (await env.ok("POST", f"/api/schedules/{sid}/run"))["run"]["task_id"]
        await env.pool.execute("UPDATE tasks SET status = 'completed' WHERE id = $1", task_id)
        counting, go = asyncio.Event(), asyncio.Event()
        count_outcome = db_schedules.count_outcome

        async def held(conn: Any, *args: Any, **kwargs: Any) -> None:
            counting.set()  # the run's row is written; the schedule's counters come next
            await go.wait()
            await count_outcome(conn, *args, **kwargs)

        monkeypatch.setattr(db_schedules, "count_outcome", held)
        caplog.clear()
        ending = asyncio.create_task(env.scheduler.task_ended(task_id, "completed"))
        await counting.wait()
        deleting = asyncio.create_task(env.call("DELETE", f"/api/schedules/{sid}"))
        await _waits_for_a_lock(env.pool, "DELETE FROM schedules")
        go.set()
        status, payload = await deleting
        await ending
        await env.scheduler.idle()
        assert (status, payload) == (200, {"deleted": sid})
        assert await env.pool.fetchval("SELECT schedule_id FROM tasks WHERE id = $1", task_id) is None
        await _gone(env, sid)
        assert _errors(caplog) == []

    caplog.set_level(logging.INFO)
    _run(test, monkeypatch, tmp_path)


def test_a_message_about_a_run_of_a_schedule_deleted_meanwhile_is_dropped_without_an_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """The notifier read the schedule of a run that ended, and the schedule is deleted before its message is
    recorded: the message is not recorded (it went with the schedule), and the notifier raises nothing."""
    from looper.db import schedules as db_schedules

    async def test(env: _Env) -> None:
        sender = _setup(env)
        await _allow_telegram(env)
        sid = (await env.enabled(notify="always", notify_target={"channel": "telegram", "chat": TELEGRAM_CHAT})
               )["schedule_id"]
        run = (await env.ok("POST", f"/api/schedules/{sid}/run"))["run"]
        async with env.pool.acquire() as conn:
            assert await db_schedules.finish_run(conn, run["run_id"], status="completed", at=env.clock.now)

        async def deleted_while_sending(channel: str, chat: str, text: str) -> tuple[bool, str | None]:
            await env.ok("DELETE", f"/api/schedules/{sid}")
            return await sender(channel, chat, text)

        env.scheduler._notifier._sender = deleted_while_sending
        await env.scheduler._notifier.run_ended(run["run_id"])
        await _gone(env, sid)

    _run(test, monkeypatch, tmp_path)
