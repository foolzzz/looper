"""The console and Manager list tasks through one core, retaining each entry's paging contract."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from tests.db_support import DSN, run_db
from tests.test_ops_tasks_db import _Chat, _project, _task
from tests.test_overview_db import _get, _server

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")


def test_console_and_manager_task_lists_keep_their_filters_and_compact_keys(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    from looper.agents import projects
    from looper.agents.http_server import _serialize

    async def test(pool: Any) -> None:
        project = await _project(pool)
        await _task(pool, "task-a", "running", project, requirement="build calc")
        await _task(pool, "task-b", "init", project, requirement="write docs")
        await _task(pool, "task-c", "failed", project, requirement="check docs")

        compact = await projects.list_tasks_page(limit=1, query="  DOCS  ", compact=True)
        chat = _Chat(tmp_path)
        searched, bounded, ready = await chat.say(
            "看看", ("looper_tasks", {"action": "list", "limit": 1, "query": "  DOCS  "}),
            ("looper_tasks", {"action": "list", "limit": 1}),
            ("looper_tasks", {"action": "list", "group": "ready", "project": "CALC"}))
        assert _serialize(compact) == searched["result"]
        assert [row["task_id"] for row in compact["tasks"]] == ["task-c"]
        assert compact["more"] is True
        assert set(compact["tasks"][0]) == {"task_id", "name", "status", "group", "project", "created_at"}
        assert bounded["result"]["more"] is False  # the tool's existing bounded-list contract
        assert [row["task_id"] for row in ready["result"]["tasks"]] == ["task-b"]

        status, page = await _get(_server(), "/api/v1/tasks/page", {"limit": 1})
        assert status == 200, page
        assert page["next_before"] == "task-c"
        assert page["counts"] == {"running": 1, "init": 1, "failed": 1}
        assert [row["task_id"] for row in page["tasks"]] == ["task-c"]
        assert {"cost", "manager_name", "requirement", "project_id"} <= set(page["tasks"][0])

    run_db(test, monkeypatch, timeout=30)
