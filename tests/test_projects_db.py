"""Projects against a real PostgreSQL: the backfill, CRUD and task creation.

Runs only when LOOPX_TEST_DATABASE_URL points at a disposable database. Each
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

DSN = os.environ.get("LOOPX_TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not DSN, reason="LOOPX_TEST_DATABASE_URL is not set")

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

    schema = f"loopx_test_{uuid.uuid4().hex[:12]}"
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
    from loopx.db.connection import ensure_schema

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
    from loopx.db.connection import ensure_schema

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

    from loopx.db import projects, tasks

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

