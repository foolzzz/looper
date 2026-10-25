"""The task detail and list say which Manager created a task and from which template (RFC §10)."""

from __future__ import annotations

from typing import Any

import pytest

from tests.db_support import DSN, run_db
from tests.test_managers_schema_db import _manager
from tests.test_task_agents_db import _call

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")

TEMPLATE = {"key": "pr", "name": "PR review", "updated_at": "2026-01-01T00:00:00Z",
            "inputs": [{"key": "url", "label": "Link", "value": "https://github.com/o/r/pull/7"}],
            "planner_instructions": "Read the diff."}


def test_task_detail_and_list_name_the_manager_and_template(monkeypatch: pytest.MonkeyPatch) -> None:
    from looper.db import projects, tasks

    async def body(pool: Any) -> None:
        await _manager(pool, "review", name="Code review")
        project = await projects.create_project(name="p", pool=pool)
        await tasks.create_task(task_id="task-a", requirement="x", project_id=project["id"],
                                manager_id="manager-review", template=TEMPLATE, pool=pool)
        await tasks.create_task(task_id="task-b", requirement="y", project_id=project["id"], pool=pool)

        status, detail = await _call("POST", "/api/v1/tasks/detail", {"task_id": "task-a"})
        assert status == 200
        assert (detail["manager_id"], detail["manager_name"]) == ("manager-review", "Code review")
        assert detail["template"] == TEMPLATE

        status, default = await _call("POST", "/api/v1/tasks/detail", {"task_id": "task-b"})
        assert status == 200
        assert default["manager_id"] == "manager" and default["template"] is None
        assert default["manager_name"]

        status, listed = await _call("POST", "/api/v1/tasks/page", {})
        assert status == 200
        rows = {t["task_id"]: t for t in listed["tasks"]}
        assert (rows["task-a"]["manager_id"], rows["task-a"]["manager_name"]) == ("manager-review", "Code review")
        assert rows["task-a"]["template"] == TEMPLATE
        assert rows["task-b"]["manager_id"] == "manager" and rows["task-b"]["template"] is None

    run_db(body, monkeypatch)
