"""The task page's task detail on a real PostgreSQL: each step's result is an object, as the plan reads it.

Runs only when LOOPER_TEST_DATABASE_URL points at a disposable database.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from tests.db_support import DSN, run_db
from tests.test_ops_tasks_db import _project, _task
from tests.test_planner_tools_db import _steps
from tests.test_task_agents_db import _call

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")


def test_the_task_detail_gives_each_steps_result_as_an_object_or_null(monkeypatch: pytest.MonkeyPatch) -> None:
    from looper.db import plans

    async def test(pool: Any) -> None:
        project = await _project(pool)
        await _task(pool, "task-a", "running", project)
        await _steps(pool, "task-a", s1="verified", s2="pending")
        result = {"status": "completed", "summary": "Wrote greet.py.", "changed_files": {"greet.py": "written"}}
        await pool.execute("UPDATE task_steps SET result = $1::jsonb WHERE id = 'step-s1'", json.dumps(result))

        status, detail = await _call("POST", "/api/v1/tasks/detail", {"task_id": "task-a"})
        assert status == 200, detail
        assert [(s["step_key"], s["result"]) for s in detail["steps"]] == [("s1", result), ("s2", None)]
        # The same step as the plan reads it.
        await pool.execute("INSERT INTO task_plans (task_id, plan_id, status) VALUES ('task-a', 'plan-a', 'executing')")
        assert [s["result"] for s in (await plans.load("task-a", pool=pool))["steps"]] == [result, None]

    run_db(test, monkeypatch)


def test_console_and_manager_share_task_detail_without_changing_the_compact_view(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from looper.agents import projects
    from looper.agents.ops.tasks import _task_view
    from looper.agents.tiers.journal import clip
    from looper.db import journal, task_changes

    async def test(pool: Any) -> None:
        project = await _project(pool)
        await _task(pool, "task-a", "running", project)
        await _steps(pool, "task-a", s1="verified", s2="pending")
        await pool.execute("UPDATE task_steps SET instruction = $1 WHERE id = 'step-s1'", "i" * 220)
        await pool.execute("UPDATE tasks SET result_summary = $1 WHERE id = 'task-a'", "r" * 1_020)
        await task_changes.add_supplement("task-a", "s" * 320, via="chat", pool=pool)
        await journal.insert(pool, "task-a", [{"step_id": "", "kind": "progress", "summary": "making progress"}])

        compact = await projects.task_detail("task-a", compact=True)
        assert compact == await _task_view("task-a")
        assert set(compact) == {
            "task_id", "name", "status", "group", "project", "created_at", "phase", "work_dir", "requirement",
            "steps", "waits", "supplements", "latest_progress", "result", "cost_usd", "concurrency",
        }
        assert compact["steps"][0] == {
            "step": "s1", "status": "verified", "kind": "work", "instruction": clip("i" * 220, 200),
        }
        assert compact["supplements"] == [{"text": clip("s" * 320, 300), "delivered": False}]
        assert compact["latest_progress"] == "making progress"
        assert compact["result"] == clip("r" * 1_020, 1_000)

        status, detail = await _call("POST", "/api/v1/tasks/detail", {"task_id": "task-a"})
        assert status == 200, detail
        assert detail["task_id"] == compact["task_id"]
        assert detail["steps"][0]["instruction"] == "i" * 220
        assert detail["supplements"][0]["text"] == "s" * 320
        assert detail["result_summary"] == "r" * 1_020

    run_db(test, monkeypatch, timeout=30)
