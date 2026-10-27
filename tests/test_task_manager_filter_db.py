"""Owner filters compose with the real task list queries on a disposable PostgreSQL."""

from datetime import datetime, timezone

import pytest

from looper.agents import projects
from looper.db import projects as db_projects, schedules, tasks
from tests.db_support import DSN, run_db

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")

OWNER = "manager-review"


async def _seed(pool):
    await pool.execute("INSERT INTO managers (id,name,system_prompt) VALUES ($1,'Review','Review tasks.')", OWNER)
    return await db_projects.create_project(name="calculator", work_dir="/workspace", pool=pool)


async def _task(pool, project, task_id, *, owner=OWNER, status="init", requirement="build calculator",
                schedule_id=None):
    await tasks.create_task(task_id=task_id, requirement=requirement, project_id=project["id"], manager_id=owner,
                            status=status, schedule_id=schedule_id, pool=pool)


def test_rows_and_status_counts_share_the_exact_owner_predicate(monkeypatch):
    async def test(pool):
        project = await _seed(pool)
        await pool.execute("INSERT INTO managers (id,name,system_prompt) VALUES ('manager-review-extra','Extra','Be.')")
        await _task(pool, project, "task-own-ready")
        await _task(pool, project, "task-own-failed", status="failed")
        await _task(pool, project, "task-default", owner="manager")
        await _task(pool, project, "task-prefix", owner="manager-review-extra")
        rows = await tasks.list_tasks(manager_id=OWNER, pool=pool)
        assert {row["id"] for row in rows} == {"task-own-ready", "task-own-failed"}
        assert await tasks.count_by_status(manager_id=OWNER, pool=pool) == {"init": 1, "failed": 1}
        page = await projects.list_tasks_page(manager_id=OWNER, status="init")
        assert [row["task_id"] for row in page["tasks"]] == ["task-own-ready"]
        assert page["counts"] == {"init": 1, "failed": 1}
    run_db(test, monkeypatch, timeout=30)


def test_owner_composes_with_project_group_schedule_and_literal_query(monkeypatch):
    async def test(pool):
        project = await _seed(pool)
        other = await db_projects.create_project(name="other", work_dir="/workspace", pool=pool)
        sid = await schedules.create_schedule(pool, project_id=project["id"], fields={
            "name": "Nightly", "cron": "0 2 * * *", "timezone": "UTC", "requirement": "Build"}, profiles={})
        await _task(pool, project, "task-match", requirement="Build 100%_ calculator", schedule_id=sid)
        await _task(pool, project, "task-owner", owner="manager", requirement="100%_", schedule_id=sid)
        await _task(pool, other, "task-project", requirement="100%_", schedule_id=sid)
        await _task(pool, project, "task-group", status="failed", requirement="100%_", schedule_id=sid)
        await _task(pool, project, "task-schedule", requirement="100%_")
        await _task(pool, project, "task-query", requirement="100XX", schedule_id=sid)
        page = await projects.list_tasks_page(manager_id=OWNER, project_id=project["id"],
            group=tasks.TaskGroup.READY, schedule_id=sid, query="  100%_  ")
        assert [row["task_id"] for row in page["tasks"]] == ["task-match"]
        assert page["counts"] == {"init": 1}
    run_db(test, monkeypatch, timeout=30)


def test_cursor_ties_keep_owner_rows_and_counts_across_pages(monkeypatch):
    async def test(pool):
        project = await _seed(pool)
        for task_id in ("task-a", "task-c", "task-e"):
            await _task(pool, project, task_id)
        await _task(pool, project, "task-d", owner="manager")
        await pool.execute("UPDATE tasks SET created_at=$1", datetime(2026, 1, 1, tzinfo=timezone.utc))
        first = await projects.list_tasks_page(manager_id=OWNER, limit=1)
        assert [row["task_id"] for row in first["tasks"]] == ["task-e"]
        assert first["next_before"] == "task-e"
        second = await projects.list_tasks_page(manager_id=OWNER, limit=1, before=first["next_before"])
        assert [row["task_id"] for row in second["tasks"]] == ["task-c"]
        assert second["next_before"] == "task-c"
        last = await projects.list_tasks_page(manager_id=OWNER, limit=1, before=second["next_before"])
        assert [row["task_id"] for row in last["tasks"]] == ["task-a"]
        assert last["next_before"] is None
        assert first["counts"] == second["counts"] == last["counts"] == {"init": 3}
    run_db(test, monkeypatch, timeout=30)


def test_omitted_owner_lists_all_but_explicit_default_lists_only_default(monkeypatch):
    async def test(pool):
        project = await _seed(pool)
        await _task(pool, project, "task-scenario")
        await _task(pool, project, "task-default", owner="manager")
        all_tasks = await projects.list_tasks_page()
        default = await projects.list_tasks_page(manager_id="manager")
        assert {row["task_id"] for row in all_tasks["tasks"]} == {"task-scenario", "task-default"}
        assert all_tasks["counts"] == {"init": 2}
        assert [row["task_id"] for row in default["tasks"]] == ["task-default"]
        assert default["counts"] == {"init": 1}
    run_db(test, monkeypatch, timeout=30)


@pytest.mark.parametrize("owner_state", ["unknown", "deleted"])
def test_unavailable_owner_never_widens_the_filter(monkeypatch, owner_state):
    async def test(pool):
        project = await _seed(pool)
        await _task(pool, project, "task-default", owner="manager")
        await _task(pool, project, "task-historical")
        if owner_state == "deleted":
            await pool.execute("UPDATE managers SET enabled=false, deleted_at=now() WHERE id=$1", OWNER)
        owner = OWNER if owner_state == "deleted" else "manager-unknown"
        assert (await projects.list_tasks_page())["counts"] == {"init": 2}
        assert await projects.list_tasks_page(manager_id=owner) == {"tasks": [], "counts": {}, "next_before": None}
        assert await projects.list_tasks_page(manager_id=owner, compact=True) == {"tasks": [], "more": False}
    run_db(test, monkeypatch, timeout=30)


def test_disabled_owner_still_lists_its_historical_tasks(monkeypatch):
    async def test(pool):
        project = await _seed(pool)
        await _task(pool, project, "task-own")
        await pool.execute("UPDATE managers SET enabled=false WHERE id=$1", OWNER)
        page = await projects.list_tasks_page(manager_id=OWNER)
        assert [row["task_id"] for row in page["tasks"]] == ["task-own"]
        assert page["counts"] == {"init": 1}
    run_db(test, monkeypatch, timeout=30)


def test_compact_search_bounds_recent_tasks_after_filtering_owner(monkeypatch):
    async def test(pool):
        project = await _seed(pool)
        await _task(pool, project, "task-old-match", requirement="needle")
        for task_id, requirement in (("task-new-a", "needle"), ("task-new-b", "needle")):
            await _task(pool, project, task_id, requirement=requirement)
        await _task(pool, project, "task-other", owner="manager", requirement="needle")
        compact = await projects.list_tasks_page(manager_id=OWNER, query="  NEEDLE  ", compact=True,
                                                 limit=1, search_limit=2)
        assert [row["task_id"] for row in compact["tasks"]] == ["task-new-b"]
        assert compact["more"] is True
        await pool.execute("UPDATE tasks SET requirement='unrelated' WHERE id IN ('task-new-a','task-new-b')")
        assert await projects.list_tasks_page(manager_id=OWNER, query="needle", compact=True,
                                             limit=1, search_limit=2) == {"tasks": [], "more": False}
        assert [row["task_id"] for row in (await projects.list_tasks_page(manager_id=OWNER,
            query="needle"))["tasks"]] == ["task-old-match"]
    run_db(test, monkeypatch, timeout=30)


def test_compact_unsearched_more_keeps_the_existing_bounded_contract(monkeypatch):
    async def test(pool):
        project = await _seed(pool)
        await _task(pool, project, "task-a")
        await _task(pool, project, "task-b")
        compact = await projects.list_tasks_page(manager_id=OWNER, compact=True, limit=1)
        assert [row["task_id"] for row in compact["tasks"]] == ["task-b"]
        assert compact["more"] is False
        assert set(compact["tasks"][0]) == {"task_id", "name", "status", "group", "project", "created_at"}
    run_db(test, monkeypatch, timeout=30)
