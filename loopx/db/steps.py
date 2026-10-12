"""Task step CRUD operations."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

import asyncpg

from .connection import get_pool


async def upsert_steps(
    task_id: str,
    steps: list[dict[str, Any]],
    *,
    pool: asyncpg.Pool | None = None,
) -> None:
    pool = pool or get_pool()
    now = datetime.now(timezone.utc)
    async with pool.acquire() as conn:
        async with conn.transaction():
            for step in steps:
                await conn.execute(
                    """
                    INSERT INTO task_steps (id, task_id, step_index, instruction,
                                            target_files, status, created_at, updated_at)
                    VALUES ($1, $2, $3, $4, $5, $6, $7, $7)
                    ON CONFLICT (id) DO UPDATE
                    SET instruction = EXCLUDED.instruction,
                        target_files = EXCLUDED.target_files,
                        updated_at = $7
                    """,
                    step["id"], task_id, step["index"], step["instruction"],
                    step.get("target_files", []), step.get("status", "pending"),
                    now,
                )


async def update_step_status(
    step_id: str,
    status: str,
    *,
    worker_id: str | None = None,
    result: dict[str, Any] | None = None,
    pool: asyncpg.Pool | None = None,
) -> None:
    pool = pool or get_pool()
    now = datetime.now(timezone.utc)
    completed_at = now if status in ("completed", "failed", "skipped", "verified") else None
    result_json = json.dumps(result) if result else None

    await pool.execute(
        """
        UPDATE task_steps
        SET status = $2,
            worker_id = COALESCE($3, worker_id),
            result = COALESCE($4::jsonb, result),
            updated_at = $5,
            completed_at = COALESCE($6, completed_at)
        WHERE id = $1
        """,
        step_id, status, worker_id, result_json, now, completed_at,
    )


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
    return [dict(r) for r in rows]


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
