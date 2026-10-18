"""Project CRUD: every task belongs to one project.

A project may give its tasks a default directory (``work_dir``, ``''`` for
none) and a ``background`` its task agents get in their system prompt; ``''``
is none. Names are unique ignoring case; a taken name raises
``asyncpg.UniqueViolationError``. Its execution settings (``EXECUTION_COLUMNS``,
typed and validated by ``looper.agents.project_execution``) are NULL to inherit.
Its agent settings are layers of their own (``agent_setting_layers``). The Agent Profiles its tasks' Planners may
choose for their Workers and Reviewers are a list of their own (``project_allowed_profiles``), empty by default.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Mapping
from typing import Any

import asyncpg

from .connection import get_pool
from .tasks import ACTIVE_STATUSES

# How the project's task agents run; NULL inherits (looper/agents/project_execution.py).
EXECUTION_COLUMNS = (
    "task_budget_usd", "task_budget_action", "supervise_interval_seconds", "review_policy",
    "distill_lessons", "step_workspaces", "workspace_links", "max_step_retries", "planner_roles",
    "role_text_max_chars", "user_gates", "gate_reminder_hours", "agent_room", "room_post_per_minute",
    "room_broadcasts_per_run", "room_message_max_chars", "room_messages_per_task", "room_ping_pong_rounds",
    "room_wait_max_seconds", "room_consult_budget_usd", "room_consult_timeout_seconds", "room_consults_per_task",
    "task_workspaces", "operator_notes",
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
           count(t.id) FILTER (WHERE t.status = ANY($1::text[])) AS active_count,
           (SELECT count(*) FROM schedules s WHERE s.project_id = p.id) AS schedule_count
    FROM projects p LEFT JOIN tasks t ON t.project_id = p.id
"""


async def list_projects(*, pool: asyncpg.Pool | None = None) -> list[dict[str, Any]]:
    """Every project with its ``task_count``, ``active_count`` and ``schedule_count``, ordered by name ignoring
    case."""
    pool = pool or get_pool()
    rows = await pool.fetch(_WITH_COUNTS + "GROUP BY p.id ORDER BY lower(p.name)", list(ACTIVE_STATUSES))
    return [dict(r) for r in rows]


async def get_project(project_id: str, *, pool: asyncpg.Pool | None = None) -> dict[str, Any] | None:
    """One project with its ``task_count``, ``active_count`` and ``schedule_count``."""
    pool = pool or get_pool()
    row = await pool.fetchrow(_WITH_COUNTS + "WHERE p.id = $2 GROUP BY p.id", list(ACTIVE_STATUSES), project_id)
    return dict(row) if row else None


async def get_task_project(task_id: str, *, pool: asyncpg.Pool | None = None) -> dict[str, Any] | None:
    """The project task ``task_id`` belongs to now (it can move); None when there is no such task. Its
    ``task_budget_usd`` is the task's own cap when it has one (a scheduled run's ``run_budget_usd``), and
    ``task_schedule_id`` the schedule that started the task (None for one a user started)."""
    pool = pool or get_pool()
    row = await pool.fetchrow(
        "SELECT p.*, t.budget_usd AS task_own_budget_usd, t.schedule_id AS task_schedule_id "
        "FROM tasks t JOIN projects p ON p.id = t.project_id WHERE t.id = $1", task_id,
    )
    if row is None:
        return None
    project = dict(row)
    if (own := project.pop("task_own_budget_usd")) is not None:
        project["task_budget_usd"] = own
    return project


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
    execution: Mapping[str, Any] | None = None,
    pool: asyncpg.Pool | asyncpg.Connection | None = None,
) -> dict[str, Any]:
    pool = pool or get_pool()
    settings = _execution(execution)
    columns = ["id", "name", "description", "work_dir", "background", *settings]
    values = [f"proj-{uuid.uuid4().hex[:12]}", name, description, work_dir, background, *settings.values()]
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
    execution: Mapping[str, Any] | None = None,
    pool: asyncpg.Pool | None = None,
) -> bool:
    """Set the fields given (not ``None``) and every execution column in ``execution`` (``None`` there
    goes back to inheriting); False when there is no such project."""
    pool = pool or get_pool()
    settings = _execution(execution)
    assignments = "".join(f"{column} = ${i}, " for i, column in enumerate(settings, start=6))
    result = await pool.execute(
        f"""
        UPDATE projects
        SET name = COALESCE($2, name),
            description = COALESCE($3, description),
            work_dir = COALESCE($4, work_dir),
            background = COALESCE($5, background),
            {assignments}updated_at = now()
        WHERE id = $1
        """,
        project_id, name, description, work_dir, background, *settings.values(),
    )
    return result == "UPDATE 1"


async def worktree_projects(*, pool: asyncpg.Pool | asyncpg.Connection | None = None) -> list[dict[str, Any]]:
    """The projects whose parallel steps work in worktrees of their own, by name: their default directory and links."""
    pool = pool or get_pool()
    rows = await pool.fetch("SELECT id, name, work_dir, workspace_links FROM projects "
                            "WHERE step_workspaces = 'worktree' ORDER BY name")
    return [dict(row) for row in rows]


async def delete_project(project_id: str, *, pool: asyncpg.Pool | None = None) -> bool:
    """Delete a project; one that still has tasks or schedules raises ``asyncpg.ForeignKeyViolationError``."""
    pool = pool or get_pool()
    result = await pool.execute("DELETE FROM projects WHERE id = $1", project_id)
    return result == "DELETE 1"


async def allowed_profiles(project_id: str, *, pool: asyncpg.Pool | asyncpg.Connection | None = None,
                           ) -> list[dict[str, Any]]:
    """The Agent Profiles the project's Planners may choose (``profile_registry.PROFILE_JSON``), by name."""
    from .profile_registry import PROFILE_JOIN, PROFILE_JSON

    pool = pool or get_pool()
    rows = await pool.fetch(
        f"SELECT {PROFILE_JSON} AS profile FROM project_allowed_profiles l JOIN {PROFILE_JOIN} ON a.id = l.profile_id "
        "WHERE l.project_id = $1 ORDER BY a.name", project_id,
    )
    return [json.loads(r["profile"]) if isinstance(r["profile"], str) else r["profile"] for r in rows]


async def set_allowed_profiles(project_id: str, profile_ids: list[str], *, pool: asyncpg.Pool | None = None) -> None:
    """Make ``profile_ids`` the whole list of the profiles the project's Planners may choose; what its tasks'
    Planners chose of a profile it drops goes with it. Raises ``asyncpg.ForeignKeyViolationError`` for an unknown
    project or profile."""
    pool = pool or get_pool()
    async with pool.acquire() as conn, conn.transaction():
        await conn.execute("DELETE FROM project_allowed_profiles WHERE project_id = $1 AND NOT profile_id = ANY($2)",
                           project_id, profile_ids)
        await conn.execute("DELETE FROM task_planner_profiles c USING tasks k WHERE k.id = c.task_id "
                           "AND k.project_id = $1 AND NOT c.profile_id = ANY($2)", project_id, profile_ids)
        await conn.executemany("INSERT INTO project_allowed_profiles (project_id, profile_id) VALUES ($1, $2) "
                               "ON CONFLICT DO NOTHING", [(project_id, profile_id) for profile_id in profile_ids])
