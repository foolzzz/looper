"""Reading task steps; the Planner writes them through ``plans``."""

from __future__ import annotations

from typing import Any

import asyncpg

from .connection import get_pool
from .tasks import step_row


async def list_steps(
    task_id: str,
    *,
    pool: asyncpg.Pool | None = None,
) -> list[dict[str, Any]]:
    pool = pool or get_pool()
    rows = await pool.fetch(
        "SELECT * FROM task_steps WHERE task_id = $1 ORDER BY step_index",
        task_id,
    )
    return [step_row(r) for r in rows]


async def list_steps_for_tasks(
    task_ids: list[str],
    *,
    pool: asyncpg.Pool | None = None,
) -> list[dict[str, Any]]:
    """The steps of several tasks, with the Worker each one ran in last."""
    if not task_ids:
        return []
    pool = pool or get_pool()
    rows = await pool.fetch(
        "SELECT id, task_id, step_index, instruction, worker_id FROM task_steps "
        "WHERE task_id = ANY($1::text[]) ORDER BY task_id, step_index",
        task_ids,
    )
    return [dict(r) for r in rows]
