"""Task CRUD operations."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import asyncpg

from .connection import get_pool

# A task in one of these is queued or running.
ACTIVE_STATUSES = ("pending", "assigned", "running", "executing")

# A task row plus its project's name.
_WITH_PROJECT = "SELECT t.*, p.name AS project_name FROM tasks t JOIN projects p ON p.id = t.project_id"


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
    import json
    pool = pool or get_pool()
    now = datetime.now(timezone.utc)
    config_json = json.dumps(config or {})
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
    pool: asyncpg.Pool | None = None,
) -> None:
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
            completed_at = COALESCE($7, completed_at)
        WHERE id = $1
        """,
        task_id, status, planner_id, result_summary, error, now, completed_at,
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
    return dict(row)


async def get_task_with_steps(task_id: str, *, pool: asyncpg.Pool | None = None) -> dict[str, Any] | None:
    pool = pool or get_pool()
    task_row = await pool.fetchrow(f"{_WITH_PROJECT} WHERE t.id = $1", task_id)
    if task_row is None:
        return None

    step_rows = await pool.fetch(
        "SELECT * FROM task_steps WHERE task_id = $1 ORDER BY step_index",
        task_id,
    )
    result = dict(task_row)
    result["steps"] = [dict(r) for r in step_rows]
    return result


async def delete_task(
    task_id: str,
    *,
    pool: asyncpg.Pool | None = None,
) -> bool:
    pool = pool or get_pool()
    await pool.execute("DELETE FROM task_steps WHERE task_id = $1", task_id)
    await pool.execute("DELETE FROM activity WHERE task_id = $1", task_id)
    result = await pool.execute("DELETE FROM tasks WHERE id = $1", task_id)
    return result == "DELETE 1"


async def reset_task_for_restart(
    task_id: str,
    *,
    pool: asyncpg.Pool | None = None,
) -> bool:
    pool = pool or get_pool()
    await pool.execute("DELETE FROM task_steps WHERE task_id = $1", task_id)
    result = await pool.execute(
        """
        UPDATE tasks
        SET status = 'pending', phase = 'pending',
            error = NULL, completed_at = NULL,
            result_summary = NULL, updated_at = now()
        WHERE id = $1
        """,
        task_id,
    )
    return result == "UPDATE 1"


async def mark_orphaned_tasks_stopped(
    *,
    pool: asyncpg.Pool | None = None,
) -> int:
    pool = pool or get_pool()
    result = await pool.execute(
        """
        UPDATE tasks
        SET status = 'stopped', updated_at = now()
        WHERE status = ANY($1::text[]) AND completed_at IS NULL
        """,
        list(ACTIVE_STATUSES),
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
    import json
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
        args.append(json.dumps(config))
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
    pool: asyncpg.Pool | None = None,
) -> list[dict[str, Any]]:
    """The newest tasks, each with its ``project_name``; only one project's when ``project_id`` is given."""
    pool = pool or get_pool()
    rows = await pool.fetch(
        f"""
        {_WITH_PROJECT}
        WHERE $3::text IS NULL OR t.project_id = $3
        ORDER BY t.created_at DESC LIMIT $1 OFFSET $2
        """,
        limit, offset, project_id,
    )
    return [dict(r) for r in rows]
