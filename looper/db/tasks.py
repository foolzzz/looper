"""Task CRUD operations."""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import datetime, timezone
from enum import Enum
from typing import Any

import asyncpg

from . import journal as db_journal
from .connection import get_pool

# A task in one of these is queued or running.
ACTIVE_STATUSES = ("pending", "assigned", "running", "executing")
# A ready task: created (from the dashboard or by the Manager) and never started. Only a user starts it
# (``start_ready_task``); a server start never resumes it.
READY = "init"

# A task row plus its project's name and task spend cap.
_WITH_PROJECT = ("SELECT t.*, p.name AS project_name, p.task_budget_usd "
                 "FROM tasks t JOIN projects p ON p.id = t.project_id")


def planner_agent_id(task_id: str) -> str:
    """The agent id of ``task_id``'s Planner, which keys its saved session."""
    return f"planner-{task_id}"


class FailKind(str, Enum):
    """Why a failed task failed, when code can tell (``tasks.fail_kind``)."""

    TASK_BUDGET = "task_budget"  # it reached its project's task spend cap (task_budget_usd)


# The most times a stored config is unwrapped from a JSON string (see _read_config).
_CONFIG_UNWRAP_LIMIT = 8


def _config_json(config: Mapping[str, Any] | None) -> str:
    """``config`` for the JSONB ``config`` column: an object, never a string."""
    if config is None:
        return "{}"
    if not isinstance(config, Mapping):
        raise TypeError(f"a task config must be an object, not {type(config).__name__}")
    return json.dumps(dict(config))


def _read_config(value: Any) -> dict[str, Any]:
    """A stored config as an object.

    asyncpg reads JSONB as its JSON text. Rows saved from the task page before
    configs were checked hold the object as a JSON string, wrapped once more
    on every save, so strings are unwrapped; anything that is not an object
    in the end reads as ``{}``.
    """
    for _ in range(_CONFIG_UNWRAP_LIMIT):
        if isinstance(value, dict):
            return value
        if not isinstance(value, str):
            break
        try:
            value = json.loads(value)
        except ValueError:
            break
    return {}


def _task(row: Mapping[str, Any]) -> dict[str, Any]:
    task = dict(row)
    if "config" in task:
        task["config"] = _read_config(task["config"])
    return task


async def create_task(
    *,
    task_id: str,
    requirement: str,
    project_id: str,
    goal_id: str = "",
    work_dir: str = "",
    config: dict[str, Any] | None = None,
    status: str = "pending",
    correlation_id: str = "",
    origin_channel_id: str = "",
    pool: asyncpg.Pool | asyncpg.Connection | None = None,
) -> dict[str, Any]:
    pool = pool or get_pool()
    now = datetime.now(timezone.utc)
    config_json = _config_json(config)
    await pool.execute(
        """
        INSERT INTO tasks (id, requirement, goal_id, work_dir, project_id,
                           config, status, correlation_id, origin_channel_id,
                           created_at, updated_at)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $10)
        ON CONFLICT (id) DO NOTHING
        """,
        task_id, requirement, goal_id, work_dir, project_id,
        config_json, status, correlation_id, origin_channel_id, now,
    )
    return {"task_id": task_id, "created_at": now.isoformat()}


async def update_task_status(
    task_id: str,
    status: str,
    *,
    planner_id: str | None = None,
    result_summary: str | None = None,
    error: str | None = None,
    fail_kind: FailKind | None = None,
    pool: asyncpg.Pool | None = None,
) -> None:
    """Set the task's status; the fields given (not ``None``) are set too, and kept until a restart."""
    pool = pool or get_pool()
    now = datetime.now(timezone.utc)
    completed_at = now if status in ("completed", "failed", "cancelled") else None

    await pool.execute(
        """
        UPDATE tasks
        SET status = $2,
            planner_id = COALESCE($3, planner_id),
            result_summary = COALESCE($4, result_summary),
            error = COALESCE($5, error),
            updated_at = $6,
            completed_at = COALESCE($7, completed_at),
            fail_kind = COALESCE($8, fail_kind)
        WHERE id = $1
        """,
        task_id, status, planner_id, result_summary, error, now, completed_at,
        FailKind(fail_kind).value if fail_kind else None,
    )


async def update_task_phase(
    task_id: str,
    phase: str,
    *,
    pool: asyncpg.Pool | None = None,
) -> None:
    pool = pool or get_pool()
    await pool.execute(
        "UPDATE tasks SET phase = $2, updated_at = now() WHERE id = $1",
        task_id, phase,
    )


async def get_task(task_id: str, *, pool: asyncpg.Pool | None = None) -> dict[str, Any] | None:
    pool = pool or get_pool()
    row = await pool.fetchrow("SELECT * FROM tasks WHERE id = $1", task_id)
    if row is None:
        return None
    return _task(row)


async def get_task_with_steps(task_id: str, *, pool: asyncpg.Pool | None = None) -> dict[str, Any] | None:
    pool = pool or get_pool()
    task_row = await pool.fetchrow(f"{_WITH_PROJECT} WHERE t.id = $1", task_id)
    if task_row is None:
        return None

    step_rows = await pool.fetch(
        "SELECT * FROM task_steps WHERE task_id = $1 ORDER BY step_index",
        task_id,
    )
    result = _task(task_row)
    result["steps"] = [dict(r) for r in step_rows]
    result["journal"] = await db_journal.list_entries(task_id, pool=pool)
    return result


async def delete_task(
    task_id: str,
    *,
    pool: asyncpg.Pool | None = None,
) -> bool:
    pool = pool or get_pool()
    await pool.execute("DELETE FROM task_steps WHERE task_id = $1", task_id)
    await pool.execute("DELETE FROM activity WHERE task_id = $1", task_id)
    await pool.execute("DELETE FROM task_plans WHERE task_id = $1", task_id)
    await pool.execute("DELETE FROM agent_sessions WHERE agent_id = $1", planner_agent_id(task_id))
    result = await pool.execute("DELETE FROM tasks WHERE id = $1", task_id)
    return result == "DELETE 1"


async def reset_task_for_restart(
    task_id: str,
    *,
    keep_steps: bool = False,
    pool: asyncpg.Pool | None = None,
) -> bool:
    """Clear the task's outcome before it runs again.

    Planned anew, its steps, its plan and its Planner's session go; continuing
    its plan (``keep_steps``), they stay.
    """
    pool = pool or get_pool()
    if not keep_steps:
        await pool.execute("DELETE FROM task_steps WHERE task_id = $1", task_id)
        await pool.execute("DELETE FROM task_plans WHERE task_id = $1", task_id)
        await pool.execute("DELETE FROM agent_sessions WHERE agent_id = $1", planner_agent_id(task_id))
    result = await pool.execute(
        """
        UPDATE tasks
        SET status = 'pending', phase = 'pending',
            error = NULL, completed_at = NULL, fail_kind = NULL,
            result_summary = NULL, updated_at = now()
        WHERE id = $1
        """,
        task_id,
    )
    return result == "UPDATE 1"


async def start_ready_task(task_id: str, *, pool: asyncpg.Pool | None = None) -> dict[str, Any] | None:
    """Move a ready task to ``pending``, once: its row, or None when there is no such task or it is not ready."""
    pool = pool or get_pool()
    row = await pool.fetchrow(
        "UPDATE tasks SET status = 'pending', updated_at = now() WHERE id = $1 AND status = $2 RETURNING *",
        task_id, READY,
    )
    return _task(row) if row is not None else None


async def mark_orphaned_tasks_stopped(
    *,
    keep: list[str] | None = None,
    pool: asyncpg.Pool | None = None,
) -> int:
    """Mark the tasks a previous server left running as stopped, except ``keep`` (resumed instead)."""
    pool = pool or get_pool()
    result = await pool.execute(
        """
        UPDATE tasks
        SET status = 'stopped', updated_at = now()
        WHERE status = ANY($1::text[]) AND completed_at IS NULL AND NOT (id = ANY($2::text[]))
        """,
        list(ACTIVE_STATUSES), list(keep or []),
    )
    count = int(result.split()[-1]) if result else 0
    return count


async def update_task_config(
    task_id: str,
    *,
    requirement: str | None = None,
    work_dir: str | None = None,
    config: dict[str, Any] | None = None,
    pool: asyncpg.Pool | None = None,
) -> bool:
    pool = pool or get_pool()
    sets: list[str] = []
    args: list[Any] = [task_id]
    idx = 2
    if requirement is not None:
        sets.append(f"requirement = ${idx}")
        args.append(requirement)
        idx += 1
    if work_dir is not None:
        sets.append(f"work_dir = ${idx}")
        args.append(work_dir)
        idx += 1
    if config is not None:
        sets.append(f"config = ${idx}")
        args.append(_config_json(config))
        idx += 1
    if not sets:
        return False
    sets.append("updated_at = now()")
    sql = f"UPDATE tasks SET {', '.join(sets)} WHERE id = $1 AND status = 'init'"
    result = await pool.execute(sql, *args)
    return result == "UPDATE 1"


async def set_task_project(
    task_id: str,
    project_id: str,
    *,
    pool: asyncpg.Pool | None = None,
) -> bool:
    """Move a task, whatever its status, to another project; False when there is no such task."""
    pool = pool or get_pool()
    result = await pool.execute(
        "UPDATE tasks SET project_id = $2, updated_at = now() WHERE id = $1",
        task_id, project_id,
    )
    return result == "UPDATE 1"


async def list_tasks(
    *,
    limit: int = 50,
    offset: int = 0,
    project_id: str | None = None,
    status: str | None = None,
    pool: asyncpg.Pool | None = None,
) -> list[dict[str, Any]]:
    """The newest tasks, each with its ``project_name``; only one project's when ``project_id`` is given, and only
    those in ``status`` when it is."""
    pool = pool or get_pool()
    rows = await pool.fetch(
        f"""
        {_WITH_PROJECT}
        WHERE ($3::text IS NULL OR t.project_id = $3) AND ($4::text IS NULL OR t.status = $4)
        ORDER BY t.created_at DESC LIMIT $1 OFFSET $2
        """,
        limit, offset, project_id, status,
    )
    return [_task(r) for r in rows]
