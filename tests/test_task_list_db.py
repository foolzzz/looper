"""The console and Manager list tasks through one core, retaining each entry's paging contract."""

from __future__ import annotations

from datetime import datetime, timezone
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


def test_excluding_scheduled_tasks_filters_rows_and_counts_and_reports_what_it_hid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from looper.db import tasks

    async def test(pool: Any) -> None:
        project = await _project(pool)
        await pool.execute(
            "INSERT INTO schedules (id, project_id, name, cron, timezone, requirement, enabled) "
            "VALUES ('sched-a', $1, 'Report', '0 * * * *', 'UTC', 'report', false)", project["id"])
        await _task(pool, "task-own-1", "running", project, requirement="real work")
        await _task(pool, "task-own-2", "failed", project, requirement="more real work")
        for n, status in enumerate(("completed", "completed", "failed")):
            await tasks.create_task(task_id=f"task-sched-{n}", requirement="progress report", project_id=project["id"],
                                    work_dir="/w", status=status, schedule_id="sched-a", pool=pool)

        # The database layer: off by default, rows and counts agree when on.
        assert {r["id"] for r in await tasks.list_tasks(pool=pool)} == {
            "task-own-1", "task-own-2", "task-sched-0", "task-sched-1", "task-sched-2"}
        assert {r["id"] for r in await tasks.list_tasks(exclude_scheduled=True, pool=pool)} == {
            "task-own-1", "task-own-2"}
        assert sum((await tasks.count_by_status(pool=pool)).values()) == 5
        assert await tasks.count_by_status(exclude_scheduled=True, pool=pool) == {"running": 1, "failed": 1}

        async def page(**arguments: Any) -> dict[str, Any]:
            status, body = await _get(_server(), "/api/v1/tasks/page", arguments)
            assert status == 200, body
            return body

        everything = await page()  # the API default keeps every task
        assert len(everything["tasks"]) == 5 and everything["hidden_scheduled"] == 0

        hiding = await page(exclude_scheduled=True)
        assert [r["task_id"] for r in hiding["tasks"]] == ["task-own-2", "task-own-1"]
        assert hiding["counts"] == {"running": 1, "failed": 1} and hiding["hidden_scheduled"] == 3

        # Combined with project, group, status and query filters, hidden_scheduled follows the same filters.
        in_project = await page(exclude_scheduled=True, project_id=project["id"])
        assert in_project["hidden_scheduled"] == 3
        assert (await page(exclude_scheduled=True, project_id="no-such-project"))["hidden_scheduled"] == 0
        attention = await page(exclude_scheduled=True, group="attention")
        assert {r["task_id"] for r in attention["tasks"]} == {"task-own-2"} and attention["hidden_scheduled"] == 1  # only the failed one is in the group
        by_status = await page(exclude_scheduled=True, status="failed")
        assert [r["task_id"] for r in by_status["tasks"]] == ["task-own-2"]
        assert by_status["hidden_scheduled"] == 3  # status narrows the rows, not the counts
        searched = await page(exclude_scheduled=True, query="progress")
        assert searched["tasks"] == [] and searched["hidden_scheduled"] == 3
        assert (await page(exclude_scheduled=True, query="real work"))["hidden_scheduled"] == 0

        # A schedule's own list is the opposite filter, so asking for both is refused.
        status, body = await _get(_server(), "/api/v1/tasks/page", {"exclude_scheduled": True, "schedule_id": "sched-a"})
        assert status == 400, body
        assert "schedule_id" in str(body)
        scheduled = await page(schedule_id="sched-a")
        assert len(scheduled["tasks"]) == 3

    run_db(test, monkeypatch, timeout=30)


def test_statuses_returns_the_union_of_filters_and_leaves_counts_per_filter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def test(pool: Any) -> None:
        project = await _project(pool)
        for status in ("executing", "planning", "running", "waiting", "failed"):
            await _task(pool, f"task-{status}", status, project)

        status, page = await _get(_server(), "/api/v1/tasks/page", {"statuses": ["running", "failed"]})
        assert status == 200, page
        assert {row["task_id"] for row in page["tasks"]} == {
            "task-executing", "task-planning", "task-running", "task-failed",
        }
        assert page["counts"] == {"running": 3, "waiting": 1, "failed": 1}

        status, only_failed = await _get(_server(), "/api/v1/tasks/page", {"statuses": ["failed"]})
        assert status == 200, only_failed
        assert [row["task_id"] for row in only_failed["tasks"]] == ["task-failed"]
        assert only_failed["counts"] == page["counts"]

        # A name status accepts that is not a chip still matches that status alone.
        status, planning = await _get(_server(), "/api/v1/tasks/page", {"statuses": ["planning"]})
        assert status == 200, planning
        assert [row["task_id"] for row in planning["tasks"]] == ["task-planning"]
        assert planning["counts"]["waiting"] == 1

    run_db(test, monkeypatch, timeout=30)


def test_status_still_filters_one_name_and_counts_stay_complete(monkeypatch: pytest.MonkeyPatch) -> None:
    async def test(pool: Any) -> None:
        project = await _project(pool)
        for status in ("executing", "waiting", "failed"):
            await _task(pool, f"task-{status}", status, project)

        status, running = await _get(_server(), "/api/v1/tasks/page", {"status": "running"})
        assert status == 200, running
        assert [row["task_id"] for row in running["tasks"]] == ["task-executing"]
        assert running["counts"] == {"running": 1, "waiting": 1, "failed": 1}

        status, failed = await _get(_server(), "/api/v1/tasks/page", {"status": "failed"})
        assert status == 200, failed
        assert [row["task_id"] for row in failed["tasks"]] == ["task-failed"]
        assert failed["counts"] == running["counts"]

    run_db(test, monkeypatch, timeout=30)


def test_statuses_pages_with_before_across_the_union(monkeypatch: pytest.MonkeyPatch) -> None:
    async def test(pool: Any) -> None:
        project = await _project(pool)
        moments = {
            "task-init": datetime(2026, 1, 1, 0, 0, 4, tzinfo=timezone.utc),
            "task-failed": datetime(2026, 1, 1, 0, 0, 3, tzinfo=timezone.utc),
            "task-waiting": datetime(2026, 1, 1, 0, 0, 2, tzinfo=timezone.utc),
            "task-executing": datetime(2026, 1, 1, 0, 0, 1, tzinfo=timezone.utc),
        }
        await _task(pool, "task-init", "init", project)
        await _task(pool, "task-failed", "failed", project)
        await _task(pool, "task-waiting", "waiting", project)
        await _task(pool, "task-executing", "executing", project)
        for task_id, moment in moments.items():
            await pool.execute("UPDATE tasks SET created_at = $2 WHERE id = $1", task_id, moment)

        selection = {"statuses": ["init", "running", "failed"], "limit": 2}
        status, page = await _get(_server(), "/api/v1/tasks/page", selection)
        assert status == 200, page
        assert [row["task_id"] for row in page["tasks"]] == ["task-init", "task-failed"]
        assert page["next_before"] == "task-failed"
        assert page["counts"]["waiting"] == 1

        status, older = await _get(_server(), "/api/v1/tasks/page", {**selection, "before": page["next_before"]})
        assert status == 200, older
        assert [row["task_id"] for row in older["tasks"]] == ["task-executing"]
        assert older["next_before"] is None
        assert "task-waiting" not in {row["task_id"] for row in [*page["tasks"], *older["tasks"]]}

    run_db(test, monkeypatch, timeout=30)


def test_statuses_rejects_both_an_unknown_name_and_a_bad_length(monkeypatch: pytest.MonkeyPatch) -> None:
    cases = (
        ({"status": "failed", "statuses": ["running"]}, "not both"),
        ({"statuses": ["nope"]}, "nope"),
        ({"statuses": ["archived"]}, "archived"),
        ({"statuses": []}, "does not match the declared schema"),
        ({"statuses": ["init"] * 11}, "does not match the declared schema"),
    )

    async def test(pool: Any) -> None:
        for body, needle in cases:
            status, page = await _get(_server(), "/api/v1/tasks/page", body)
            assert status == 400, (body, page)
            assert needle in str(page), (body, page)

    run_db(test, monkeypatch, timeout=30)
