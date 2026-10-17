"""When a task's step started, on a real PostgreSQL: the first start of its run is kept through later saves (its
retries and reviews), a step sent back to not started (a restart) has none until it starts again, a step that never
started has none, a step from before the column keeps none (no start is guessed), and the task detail gives it.

Runs only when LOOPER_TEST_DATABASE_URL points at a disposable database.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from tests.test_ops_tasks_db import _project, _task
from tests.test_planner_tools_db import _steps
from tests.test_task_agents_db import _call
from tests.test_usage_api_db import DSN, _run

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")

FIRST = datetime(2026, 10, 17, 8, 0, 0, tzinfo=timezone.utc)


def _row(step_id: str, status: str, started_at: datetime | None, index: int = 0) -> dict[str, Any]:
    return {"id": step_id, "index": index, "key": step_id, "instruction": "do it", "status": status,
            "started_at": started_at}


async def _started(pool: Any, step_id: str) -> datetime | None:
    return await pool.fetchval("SELECT started_at FROM task_steps WHERE id = $1", step_id)


def test_a_step_keeps_the_start_of_its_first_attempt(monkeypatch: pytest.MonkeyPatch) -> None:
    from looper.db import plans

    async def test(pool: Any) -> None:
        await _task(pool, "task-a", "running", await _project(pool))
        await plans.save("task-a", steps=[_row("s1", "pending", None)], pool=pool)
        assert await _started(pool, "s1") is None  # planned, not started

        await plans.save("task-a", steps=[_row("s1", "dispatched", FIRST)], pool=pool)
        assert await _started(pool, "s1") == FIRST
        # A retry, a review: later saves keep it, with a later start or none.
        await plans.save("task-a", steps=[_row("s1", "dispatched", FIRST + timedelta(minutes=5))], pool=pool)
        await plans.save("task-a", steps=[_row("s1", "verified", None)], pool=pool)
        assert await _started(pool, "s1") == FIRST

    _run(test, monkeypatch)


def test_a_step_sent_back_to_not_started_starts_again(monkeypatch: pytest.MonkeyPatch) -> None:
    from looper.db import plans

    async def test(pool: Any) -> None:
        await _task(pool, "task-a", "running", await _project(pool))
        await plans.save("task-a", steps=[_row("s1", "failed", FIRST)], pool=pool)
        # A restart sends it back to not started: the time the task was stopped does not count.
        await plans.save("task-a", steps=[_row("s1", "pending", FIRST)], pool=pool)
        assert await _started(pool, "s1") is None
        again = FIRST + timedelta(days=2)
        await plans.save("task-a", steps=[_row("s1", "dispatched", again)], pool=pool)
        assert await _started(pool, "s1") == again

    _run(test, monkeypatch)


def test_a_step_from_before_the_column_gets_no_guessed_start(monkeypatch: pytest.MonkeyPatch) -> None:
    from looper.db import plans

    async def test(pool: Any) -> None:
        await _task(pool, "task-a", "running", await _project(pool))
        await _steps(pool, "task-a", s1="verified", s2="dispatched")  # rows written without a start
        await plans.save("task-a", steps=[_row("step-s1", "verified", None), _row("step-s2", "dispatched", None, 1)],
                         pool=pool)
        assert [await _started(pool, "step-s1"), await _started(pool, "step-s2")] == [None, None]

    _run(test, monkeypatch)


def test_the_task_detail_gives_each_steps_start(monkeypatch: pytest.MonkeyPatch) -> None:
    from looper.db import plans

    async def test(pool: Any) -> None:
        await _task(pool, "task-a", "running", await _project(pool))
        await plans.save("task-a", steps=[_row("s1", "dispatched", FIRST), _row("s2", "pending", None, 1)], pool=pool)
        status, detail = await _call("GET", "/api/tasks/task-a")
        assert status == 200, detail
        started = {s["step_key"]: s["started_at"] for s in detail["steps"]}
        assert started["s2"] is None
        assert datetime.fromisoformat(started["s1"]) == FIRST

    _run(test, monkeypatch)
