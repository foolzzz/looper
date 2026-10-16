"""Projects against a real PostgreSQL: the backfill, CRUD and task creation.

Runs only when LOOPER_TEST_DATABASE_URL points at a disposable database. Each
test works in a new schema of its own, dropped afterwards, so the backfill can
start from a database that predates projects.
"""

from __future__ import annotations

import asyncio
import os
import re
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Any

import pytest

DSN = os.environ.get("LOOPER_TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")

# The tasks table as it was before projects.
OLD_TASKS_TABLE = """
CREATE TABLE tasks (
    id                TEXT PRIMARY KEY,
    requirement       TEXT NOT NULL,
    goal_id           TEXT NOT NULL DEFAULT '',
    status            TEXT NOT NULL DEFAULT 'pending',
    phase             TEXT NOT NULL DEFAULT 'pending',
    planner_id        TEXT,
    correlation_id    TEXT NOT NULL DEFAULT '',
    origin_channel_id TEXT NOT NULL DEFAULT '',
    result_summary    TEXT,
    error             TEXT,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at      TIMESTAMPTZ,
    work_dir          TEXT NOT NULL DEFAULT '',
    config            JSONB NOT NULL DEFAULT '{}'
)
"""


@asynccontextmanager
async def _fresh_schema() -> AsyncIterator[Any]:
    """A pool whose tables live in a new, empty schema, dropped afterwards."""
    import asyncpg

    schema = f"looper_test_{uuid.uuid4().hex[:12]}"
    admin = await asyncpg.connect(DSN)
    await admin.execute(f'CREATE SCHEMA "{schema}"')
    pool = await asyncpg.create_pool(DSN, min_size=1, max_size=2, server_settings={"search_path": schema})
    try:
        yield pool
    finally:
        await pool.close()
        await admin.execute(f'DROP SCHEMA "{schema}" CASCADE')
        await admin.close()


def _run(test: Callable[[Any], Awaitable[None]], *, old_tasks: bool = False) -> None:
    from looper.db.connection import ensure_schema

    async def _main() -> None:
        async with _fresh_schema() as pool:
            if old_tasks:
                await pool.execute(OLD_TASKS_TABLE)
            else:
                await ensure_schema(pool)
            await test(pool)

    asyncio.run(_main())


async def _projects_by_task(pool: Any) -> dict[str, tuple[str, str, str]]:
    rows = await pool.fetch(
        "SELECT t.id, p.id AS project_id, p.name, p.work_dir FROM tasks t JOIN projects p ON p.id = t.project_id"
    )
    return {r["id"]: (r["project_id"], r["name"], r["work_dir"]) for r in rows}


def test_backfill_gives_every_old_task_a_project() -> None:
    from looper.db.connection import ensure_schema

    async def _test(pool: Any) -> None:
        # Older directories keep the plain name; a clash, ignoring case, gets -2, -3, ...
        for task_id, work_dir, hours_ago in [
            ("task-a1", "/a/tetris", 5), ("task-e", "", 4), ("task-b", "/b/Tetris/", 3),
            ("task-root", "/", 2), ("task-a2", "/a/tetris", 1),
        ]:
            await pool.execute(
                "INSERT INTO tasks (id, requirement, work_dir, created_at) VALUES ($1, 'r', $2, now() - $3 * interval '1 hour')",
                task_id, work_dir, hours_ago,
            )

        await ensure_schema(pool)

        placed = await _projects_by_task(pool)
        assert {task: (name, work_dir) for task, (_, name, work_dir) in placed.items()} == {
            "task-a1": ("tetris", "/a/tetris"),
            "task-a2": ("tetris", "/a/tetris"),
            "task-e": ("default", ""),
            "task-b": ("Tetris-2", "/b/Tetris/"),
            "task-root": ("default-2", "/"),
        }
        assert placed["task-a1"][0] == placed["task-a2"][0]
        assert await pool.fetchval("SELECT count(*) FROM projects") == 4
        assert all(re.fullmatch(r"proj-[0-9a-f]{12}", project_id) for project_id, _, _ in placed.values())
        nullable = await pool.fetchval(
            "SELECT is_nullable FROM information_schema.columns "
            "WHERE table_schema = current_schema() AND table_name = 'tasks' AND column_name = 'project_id'"
        )
        assert nullable == "NO"
        assert await pool.fetchval("SELECT count(*) FROM schema_migrations WHERE name = 'tasks_project_backfill'") == 1

        # Applying the schema again changes nothing.
        projects = await pool.fetch("SELECT * FROM projects ORDER BY id")
        await ensure_schema(pool)
        assert await _projects_by_task(pool) == placed
        assert await pool.fetch("SELECT * FROM projects ORDER BY id") == projects

    _run(_test, old_tasks=True)


def test_a_new_database_starts_with_no_projects() -> None:
    async def _test(pool: Any) -> None:
        assert await pool.fetchval("SELECT count(*) FROM projects") == 0
        assert await pool.fetchval("SELECT count(*) FROM schema_migrations WHERE name = 'tasks_project_backfill'") == 1

    _run(_test)


def test_project_crud_and_counts() -> None:
    import asyncpg

    from looper.db import projects, tasks

    async def _test(pool: Any) -> None:
        tetris = await projects.create_project(name="Tetris", work_dir="/srv/tetris", pool=pool)
        assert re.fullmatch(r"proj-[0-9a-f]{12}", tetris["id"])
        with pytest.raises(asyncpg.UniqueViolationError):
            await projects.create_project(name="tetris", pool=pool)
        notes = await projects.create_project(name="notes", description="d", pool=pool)

        for task_id, project, status in [("task-1", tetris, "running"), ("task-2", tetris, "completed"),
                                         ("task-3", notes, "init")]:
            await tasks.create_task(task_id=task_id, requirement="r", work_dir="/w", status=status,
                                    project_id=project["id"], pool=pool)

        listed = await projects.list_projects(pool=pool)
        assert [(p["name"], p["task_count"], p["active_count"]) for p in listed] == [
            ("notes", 1, 0), ("Tetris", 2, 1)]
        got = await projects.get_project(tetris["id"], pool=pool)
        assert (got["name"], got["work_dir"], got["task_count"], got["active_count"]) == ("Tetris", "/srv/tetris", 2, 1)
        assert await projects.get_project("proj-000000000000", pool=pool) is None
        assert (await projects.find_project_by_name("TETRIS", pool=pool))["id"] == tetris["id"]
        assert await projects.find_project_by_name("tetri", pool=pool) is None

        with pytest.raises(asyncpg.UniqueViolationError):
            await projects.update_project(notes["id"], name="TETRIS", pool=pool)
        assert await projects.update_project(tetris["id"], name="TETRIS", work_dir="", pool=pool)
        got = await projects.get_project(tetris["id"], pool=pool)
        assert (got["name"], got["description"], got["work_dir"]) == ("TETRIS", "", "")
        assert got["updated_at"] > got["created_at"]
        assert not await projects.update_project("proj-000000000000", name="x", pool=pool)

        assert await tasks.set_task_project("task-3", tetris["id"], pool=pool)
        assert not await tasks.set_task_project("task-nope", tetris["id"], pool=pool)
        in_tetris = await tasks.list_tasks(project_id=tetris["id"], pool=pool)
        assert sorted(t["id"] for t in in_tetris) == ["task-1", "task-2", "task-3"]
        assert {t["project_name"] for t in in_tetris} == {"TETRIS"}
        assert await tasks.list_tasks(project_id=notes["id"], pool=pool) == []
        assert len(await tasks.list_tasks(pool=pool)) == 3
        detail = await tasks.get_task_with_steps("task-3", pool=pool)
        assert (detail["project_id"], detail["project_name"], detail["steps"]) == (tetris["id"], "TETRIS", [])

        with pytest.raises(asyncpg.ForeignKeyViolationError):
            await projects.delete_project(tetris["id"], pool=pool)
        assert await projects.delete_project(notes["id"], pool=pool)
        assert not await projects.delete_project(notes["id"], pool=pool)

        # Only the running task was active; startup stops it.
        assert await tasks.mark_orphaned_tasks_stopped(pool=pool) == 1
        assert (await tasks.get_task("task-1", pool=pool))["status"] == "stopped"

    _run(_test)


def test_a_ready_task_is_listed_left_alone_at_startup_and_starts_once() -> None:
    from looper.db import plans, projects, tasks

    async def _test(pool: Any) -> None:
        project = await projects.create_project(name="tetris", work_dir="/srv/tetris", pool=pool)
        for task_id, status in (("task-ready", tasks.READY), ("task-run", "running")):
            await tasks.create_task(task_id=task_id, requirement="r", work_dir="/w", status=status,
                                    project_id=project["id"], pool=pool)
        assert [t["id"] for t in await tasks.list_tasks(status=tasks.READY, pool=pool)] == ["task-ready"]
        # A server start stops what the previous one left running and resumes nothing that is ready.
        assert await plans.resumable_task_ids(tasks.ACTIVE_STATUSES, pool=pool) == []
        assert await tasks.mark_orphaned_tasks_stopped(pool=pool) == 1
        assert (await tasks.get_task("task-ready", pool=pool))["status"] == tasks.READY

        started = await tasks.start_ready_task("task-ready", pool=pool)
        assert (started["id"], started["status"], started["project_id"]) == ("task-ready", "pending", project["id"])
        for task_id in ("task-ready", "task-run", "task-nope"):  # started already, not ready, unknown
            assert await tasks.start_ready_task(task_id, pool=pool) is None
        assert (await tasks.get_task("task-run", pool=pool))["status"] == "stopped"

    _run(_test)


def test_task_with_a_new_project_is_one_transaction(monkeypatch: pytest.MonkeyPatch) -> None:
    from looper.agents import projects as api
    from looper.db import connection, tasks

    async def _test(pool: Any) -> None:
        monkeypatch.setattr(connection, "_pool", pool)

        created = await api.create_task({"requirement": "r", "project": {"name": "Notes", "work_dir": "/srv/notes"}})
        row = await tasks.get_task(created["task_id"], pool=pool)
        assert (row["project_id"], row["work_dir"], row["status"]) == (created["project_id"], "/srv/notes", "init")

        with pytest.raises(api.RequestError) as taken:
            await api.create_task({"requirement": "r", "project": {"name": "notes"}, "work_dir": "/tmp"})
        assert taken.value.status.startswith("409")

        async def failing_insert(**_: Any) -> None:
            raise RuntimeError("task insert failed")

        monkeypatch.setattr(tasks, "create_task", failing_insert)
        with pytest.raises(RuntimeError):
            await api.create_task({"requirement": "r", "project": {"name": "orphan"}, "work_dir": "/tmp"})
        assert await pool.fetchval("SELECT array_agg(name) FROM projects") == ["Notes"]
        assert await pool.fetchval("SELECT count(*) FROM tasks") == 1

    _run(_test)


def test_background_column_is_added_to_an_existing_projects_table() -> None:
    from looper.db.connection import ensure_schema

    async def _main() -> None:
        async with _fresh_schema() as pool:
            # A database from before backgrounds, holding a project.
            await ensure_schema(pool)
            await pool.execute("ALTER TABLE projects DROP COLUMN background")
            await pool.execute("INSERT INTO projects (id, name) VALUES ('proj-old', 'old')")

            await ensure_schema(pool)

            column = await pool.fetchrow(
                "SELECT data_type, is_nullable, column_default FROM information_schema.columns "
                "WHERE table_schema = current_schema() AND table_name = 'projects' AND column_name = 'background'"
            )
            assert column is not None
            assert (column["data_type"], column["is_nullable"], column["column_default"]) == ("text", "NO", "''::text")
            assert await pool.fetchval("SELECT background FROM projects WHERE id = 'proj-old'") == ""
            await ensure_schema(pool)  # applying the schema again changes nothing
            assert await pool.fetchval("SELECT count(*) FROM projects") == 1

    asyncio.run(_main())


def test_background_round_trips_and_is_read_through_the_task() -> None:
    from looper.db import projects, tasks

    async def _test(pool: Any) -> None:
        text = "  - use uv\n\n  - Python 3.12 · 中文"
        calc = await projects.create_project(name="calc", background=text, pool=pool)
        assert calc["background"] == text
        assert (await projects.get_project(calc["id"], pool=pool))["background"] == text
        plain = await projects.create_project(name="plain", pool=pool)
        assert plain["background"] == ""
        assert [p["background"] for p in await projects.list_projects(pool=pool)] == [text, ""]

        # Changing another field keeps it; "" clears it.
        assert await projects.update_project(calc["id"], name="Calc", pool=pool)
        assert (await projects.get_project(calc["id"], pool=pool))["background"] == text
        assert await projects.update_project(calc["id"], background="", pool=pool)
        assert (await projects.get_project(calc["id"], pool=pool))["background"] == ""
        assert await projects.update_project(calc["id"], background="v2", pool=pool)

        # The project a task's agents read is the task's current one.
        await tasks.create_task(task_id="task-1", requirement="r", work_dir="/w", project_id=calc["id"], pool=pool)
        got = await projects.get_task_project("task-1", pool=pool)
        assert (got["id"], got["name"], got["description"], got["background"]) == (calc["id"], "Calc", "", "v2")
        assert await tasks.set_task_project("task-1", plain["id"], pool=pool)
        assert (await projects.get_task_project("task-1", pool=pool))["id"] == plain["id"]
        assert await projects.get_task_project("task-nope", pool=pool) is None

    _run(_test)


def test_execution_settings_are_stored_through_the_task_and_cleared() -> None:
    """The execution columns as the API stores them: given ones set, None back to NULL, the rest kept."""
    import asyncpg

    from looper.agents.project_execution import ProjectExecution, execution_fields
    from looper.db import projects, tasks

    async def _test(pool: Any) -> None:
        settings = execution_fields({"task_budget_usd": 2, "task_budget_action": "wrap_up", "supervise_interval_seconds": 0,
                                     "review_policy": "all", "distill_lessons": "draft", "planner_roles": "on",
                                     "role_text_max_chars": 600})
        calc = await projects.create_project(name="calc", execution=settings, pool=pool)
        await tasks.create_task(task_id="task-1", requirement="r", work_dir="/w", project_id=calc["id"], pool=pool)
        assert ProjectExecution.from_row(await projects.get_task_project("task-1", pool=pool)).view() == {
            **settings, "task_budget_usd": 2.0, "step_workspaces": None, "max_step_retries": None}

        assert await projects.update_project(calc["id"], execution={"task_budget_usd": None,
                                                                    "supervise_interval_seconds": 60}, pool=pool)
        row = await projects.get_project(calc["id"], pool=pool)
        assert (row["task_budget_usd"], row["supervise_interval_seconds"], row["review_policy"]) == (None, 60, "all")
        plain = await projects.create_project(name="plain", pool=pool)
        assert all(plain[column] is None for column in projects.EXECUTION_COLUMNS if column != "distill_lessons")
        assert plain["distill_lessons"] == "off"  # never unset
        with pytest.raises(asyncpg.CheckViolationError):  # the table refuses what validation would
            await projects.update_project(plain["id"], execution={"review_policy": "some"}, pool=pool)

    _run(_test)


def test_distill_lessons_from_before_the_modes_becomes_write_or_off() -> None:
    """The boolean from before the three modes: true becomes write, false and NULL off; new projects are off."""
    import asyncpg

    from looper.db.connection import ensure_schema

    async def _modes(pool: Any) -> dict[str, str]:
        return {r["id"]: r["distill_lessons"] for r in await pool.fetch("SELECT id, distill_lessons FROM projects")}

    async def _test(pool: Any) -> None:
        # The column as it was before the modes.
        await pool.execute("DELETE FROM schema_migrations WHERE name = 'distill_lessons_modes'")
        await pool.execute("ALTER TABLE projects DROP COLUMN distill_lessons")
        await pool.execute("ALTER TABLE projects ADD COLUMN distill_lessons BOOLEAN")
        for project_id, value in [("proj-on", True), ("proj-off", False), ("proj-unset", None)]:
            await pool.execute("INSERT INTO projects (id, name, distill_lessons) VALUES ($1, $1, $2)", project_id, value)

        await ensure_schema(pool)

        assert await _modes(pool) == {"proj-on": "write", "proj-off": "off", "proj-unset": "off"}
        await pool.execute("INSERT INTO projects (id, name) VALUES ('proj-new', 'new')")
        await pool.execute("UPDATE projects SET distill_lessons = 'draft' WHERE id = 'proj-off'")
        for value, refused in [("on", asyncpg.CheckViolationError), (None, asyncpg.NotNullViolationError)]:
            with pytest.raises(refused):
                await pool.execute("UPDATE projects SET distill_lessons = $1 WHERE id = 'proj-new'", value)

        # Applying the schema again changes nothing.
        await ensure_schema(pool)
        assert await _modes(pool) == {"proj-on": "write", "proj-off": "draft", "proj-unset": "off", "proj-new": "off"}

    _run(_test)


@pytest.mark.parametrize("wraps", [0, 1, 2])
def test_task_config_is_written_as_an_object_and_a_string_wrapped_one_reads_back(wraps: int) -> None:
    """Saves from the task page used to wrap the object in one more JSON string each time."""
    import json

    from looper.db import projects, tasks

    async def _test(pool: Any) -> None:
        project = await projects.create_project(name="p", pool=pool)
        await tasks.create_task(task_id="task-1", requirement="r", project_id=project["id"], config={"a": 1},
                                status="init", pool=pool)
        await tasks.update_task_config("task-1", config={"a": 2}, pool=pool)
        assert await pool.fetchval("SELECT jsonb_typeof(config) FROM tasks") == "object"

        stored: Any = {"a": 2}
        for _ in range(wraps):
            stored = json.dumps(stored)
        await pool.execute("UPDATE tasks SET config = $1", json.dumps(stored))
        assert (await tasks.get_task("task-1", pool=pool))["config"] == {"a": 2}
        assert (await tasks.get_task_with_steps("task-1", pool=pool))["config"] == {"a": 2}
        assert [t["config"] for t in await tasks.list_tasks(pool=pool)] == [{"a": 2}]

    _run(_test)
