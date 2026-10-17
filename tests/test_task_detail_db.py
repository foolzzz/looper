"""The task page's task detail on a real PostgreSQL: each step's result is an object, as the plan reads it.

Runs only when LOOPER_TEST_DATABASE_URL points at a disposable database.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from tests.test_ops_tasks_db import _project, _task
from tests.test_planner_tools_db import _steps
from tests.test_task_agents_db import _call
from tests.test_usage_api_db import DSN, _run

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")


def test_the_task_detail_gives_each_steps_result_as_an_object_or_null(monkeypatch: pytest.MonkeyPatch) -> None:
    from looper.db import plans

    async def test(pool: Any) -> None:
        project = await _project(pool)
        await _task(pool, "task-a", "running", project)
        await _steps(pool, "task-a", s1="verified", s2="pending")
        result = {"status": "completed", "summary": "Wrote greet.py.", "changed_files": {"greet.py": "written"}}
        await pool.execute("UPDATE task_steps SET result = $1::jsonb WHERE id = 'step-s1'", json.dumps(result))

        status, detail = await _call("GET", "/api/tasks/task-a")
        assert status == 200, detail
        assert [(s["step_key"], s["result"]) for s in detail["steps"]] == [("s1", result), ("s2", None)]
        # The same step as the plan reads it.
        await pool.execute("INSERT INTO task_plans (task_id, plan_id, status) VALUES ('task-a', 'plan-a', 'executing')")
        assert [s["result"] for s in (await plans.load("task-a", pool=pool))["steps"]] == [result, None]

    _run(test, monkeypatch)
