"""The operator's verdict on a completed task (``task_feedback``): one row per task, changed or withdrawn."""

import asyncpg

from .connection import get_pool

Database = asyncpg.Pool | asyncpg.Connection
VERDICTS = ("defect", "incomplete", "wrong_approach", "other")
NOTE_MAX_CHARS = 500


async def get(task_id: str, *, pool: Database | None = None) -> asyncpg.Record | None:
    return await (pool or get_pool()).fetchrow(
        "SELECT task_id,verdict,note,created_at,updated_at FROM task_feedback WHERE task_id=$1", task_id)


async def put(task_id: str, verdict: str, note: str | None, *, pool: Database | None = None) -> asyncpg.Record:
    """Insert or replace the task's verdict; ``created_at`` keeps the first mark, ``updated_at`` the last change."""
    return await (pool or get_pool()).fetchrow(
        "INSERT INTO task_feedback(task_id,verdict,note) VALUES ($1,$2,$3) "
        "ON CONFLICT (task_id) DO UPDATE SET verdict=EXCLUDED.verdict,note=EXCLUDED.note,updated_at=now() "
        "RETURNING task_id,verdict,note,created_at,updated_at", task_id, verdict, note)


async def withdraw(task_id: str, *, pool: Database | None = None) -> bool:
    return (await (pool or get_pool()).execute("DELETE FROM task_feedback WHERE task_id=$1", task_id)) == "DELETE 1"
