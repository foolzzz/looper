"""Task CRUD operations."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import asyncpg

from .connection import get_pool


async def create_task(
    *,
    task_id: str,
    requirement: str,
    goal_id: str = "",
    project_root: str = "",
    correlation_id: str = "",
    origin_channel_id: str = "",
    pool: asyncpg.Pool | None = None,
) -> dict[str, Any]:
    pool = pool or get_pool()
    now = datetime.now(timezone.utc)
    await pool.execute(
        """
        INSERT INTO tasks (id, requirement, goal_id, project_root,
                           correlation_id, origin_channel_id, created_at, updated_at)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $7)
        ON CONFLICT (id) DO NOTHING
        """,
        task_id, requirement, goal_id, project_root,
        correlation_id, origin_channel_id, now,
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
    task_row = await pool.fetchrow("SELECT * FROM tasks WHERE id = $1", task_id)
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


async def list_tasks(
    *, limit: int = 50, offset: int = 0, pool: asyncpg.Pool | None = None,
) -> list[dict[str, Any]]:
    pool = pool or get_pool()
    rows = await pool.fetch(
        "SELECT * FROM tasks ORDER BY created_at DESC LIMIT $1 OFFSET $2",
        limit, offset,
    )
    return [dict(r) for r in rows]
