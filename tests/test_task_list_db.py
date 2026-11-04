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


def test_running_filter_lists_planner_work_and_counts_every_task_once(monkeypatch: pytest.MonkeyPatch) -> None:
    from looper.agents import projects
    from looper.db import tasks

    async def test(pool: Any) -> None:
        project = await _project(pool)
        for status in ("executing", "planning", "running", "waiting"):
            await _task(pool, f"task-{status}", status, project)

        status, page = await _get(_server(), "/api/v1/tasks/page", {"status": "running"})
        assert status == 200, page
        assert {row["task_id"] for row in page["tasks"]} == {
            "task-executing", "task-planning", "task-running",
        }
        assert {row["status"] for row in page["tasks"]} == {"executing", "planning", "running"}
        assert page["counts"] == {"running": 3, "waiting": 1}
        all_tasks = await projects.list_tasks_page()
        assert sum(page["counts"].values()) == len(all_tasks["tasks"]) == 4
        waiting = await projects.list_tasks_page(status="waiting")
        assert [row["task_id"] for row in waiting["tasks"]] == ["task-waiting"]
        compact = await projects.list_tasks_page(status="running", compact=True)
        assert {row["task_id"] for row in compact["tasks"]} == {
            "task-executing", "task-planning", "task-running",
        }

        # Unknown filter names retain exact matching and still contribute once to All.
        await _task(pool, "task-archived", "archived", project)
        exact = await tasks.list_tasks(status="archived", pool=pool)
        assert [row["id"] for row in exact] == ["task-archived"]
        assert await tasks.list_tasks(status="missing", pool=pool) == []
        assert await tasks.count_by_status(pool=pool) == {"running": 3, "waiting": 1, "archived": 1}

    run_db(test, monkeypatch, timeout=30)
