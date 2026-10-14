"""Project CRUD: every task belongs to one project.

A project may give its tasks a default directory (``work_dir``, ``''`` for
none), a ``background`` its task agents get in their system prompt and
harness prompts for its Planner (``planner_harness_prompt``) and for its
Workers and Reviewers (``worker_harness_prompt``); ``''`` is none. Names are unique ignoring case; a taken name raises
``asyncpg.UniqueViolationError``. Its execution settings (``EXECUTION_COLUMNS``,
typed and validated by ``loopx.agents.project_execution``) are NULL to inherit.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from typing import Any

import asyncpg

from .connection import get_pool
from .tasks import ACTIVE_STATUSES

# How the project's task agents run; NULL inherits (loopx/agents/project_execution.py).
EXECUTION_COLUMNS = (
    "model", "run_budget_usd", "task_budget_usd", "task_budget_action", "supervise_interval_seconds", "review_policy",
    "distill_lessons",
)


def _execution(execution: Mapping[str, Any] | None) -> dict[str, Any]:
    values = dict(execution or {})
    unknown = sorted(set(values) - set(EXECUTION_COLUMNS))
    if unknown:
        raise ValueError(f"not an execution column: {unknown[0]}")
    return values

_WITH_COUNTS = """
    SELECT p.*,
           count(t.id) AS task_count,
           count(t.id) FILTER (WHERE t.status = ANY($1::text[])) AS active_count
    FROM projects p LEFT JOIN tasks t ON t.project_id = p.id
"""


async def list_projects(*, pool: asyncpg.Pool | None = None) -> list[dict[str, Any]]:
    """Every project with its ``task_count`` and ``active_count``, ordered by name ignoring case."""
    pool = pool or get_pool()
    rows = await pool.fetch(_WITH_COUNTS + "GROUP BY p.id ORDER BY lower(p.name)", list(ACTIVE_STATUSES))
    return [dict(r) for r in rows]


async def get_project(project_id: str, *, pool: asyncpg.Pool | None = None) -> dict[str, Any] | None:
    """One project with its ``task_count`` and ``active_count``."""
    pool = pool or get_pool()
    row = await pool.fetchrow(_WITH_COUNTS + "WHERE p.id = $2 GROUP BY p.id", list(ACTIVE_STATUSES), project_id)
    return dict(row) if row else None


async def get_task_project(task_id: str, *, pool: asyncpg.Pool | None = None) -> dict[str, Any] | None:
    """The project task ``task_id`` belongs to now (it can move); None when there is no such task."""
    pool = pool or get_pool()
    row = await pool.fetchrow(
        "SELECT p.* FROM tasks t JOIN projects p ON p.id = t.project_id WHERE t.id = $1", task_id,
    )
    return dict(row) if row else None


async def find_project_by_name(name: str, *, pool: asyncpg.Pool | None = None) -> dict[str, Any] | None:
    pool = pool or get_pool()
    row = await pool.fetchrow("SELECT * FROM projects WHERE lower(name) = lower($1)", name)
    return dict(row) if row else None


async def create_project(
    *,
    name: str,
    description: str = "",
    work_dir: str = "",
    background: str = "",
    planner_harness_prompt: str = "",
    worker_harness_prompt: str = "",
    execution: Mapping[str, Any] | None = None,
    pool: asyncpg.Pool | asyncpg.Connection | None = None,
) -> dict[str, Any]:
    pool = pool or get_pool()
    settings = _execution(execution)
    columns = ["id", "name", "description", "work_dir", "background", "planner_harness_prompt",
               "worker_harness_prompt", *settings]
    values = [f"proj-{uuid.uuid4().hex[:12]}", name, description, work_dir, background,
              planner_harness_prompt, worker_harness_prompt, *settings.values()]
    row = await pool.fetchrow(
        f"INSERT INTO projects ({', '.join(columns)}) "
        f"VALUES ({', '.join(f'${i}' for i in range(1, len(values) + 1))}) RETURNING *",
        *values,
    )
    return dict(row)


async def update_project(
    project_id: str,
    *,
    name: str | None = None,
    description: str | None = None,
    work_dir: str | None = None,
    background: str | None = None,
    planner_harness_prompt: str | None = None,
    worker_harness_prompt: str | None = None,
    execution: Mapping[str, Any] | None = None,
    pool: asyncpg.Pool | None = None,
) -> bool:
    """Set the fields given (not ``None``) and every execution column in ``execution`` (``None`` there
    goes back to inheriting); False when there is no such project."""
    pool = pool or get_pool()
    settings = _execution(execution)
    assignments = "".join(f"{column} = ${i}, " for i, column in enumerate(settings, start=8))
    result = await pool.execute(
        f"""
        UPDATE projects
        SET name = COALESCE($2, name),
            description = COALESCE($3, description),
            work_dir = COALESCE($4, work_dir),
            background = COALESCE($5, background),
            planner_harness_prompt = COALESCE($6, planner_harness_prompt),
            worker_harness_prompt = COALESCE($7, worker_harness_prompt),
            {assignments}updated_at = now()
        WHERE id = $1
        """,
        project_id, name, description, work_dir, background, planner_harness_prompt, worker_harness_prompt,
        *settings.values(),
    )
    return result == "UPDATE 1"


async def delete_project(project_id: str, *, pool: asyncpg.Pool | None = None) -> bool:
    """Delete a project; one that still has tasks raises ``asyncpg.ForeignKeyViolationError``."""
    pool = pool or get_pool()
    result = await pool.execute("DELETE FROM projects WHERE id = $1", project_id)
    return result == "DELETE 1"
