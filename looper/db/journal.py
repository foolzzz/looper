"""A task's journal (``task_journal``): one entry per accepted step, written by code.

``plans.write`` adds entries in the transaction that settles their step; the
Planner reads them back with its plan (``plans.load``) and the task page with
the task (``tasks.get_task_with_steps``).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

import asyncpg

from .connection import get_pool

# Every kind an entry can have (task_journal.kind).
KINDS = ("step_accepted",)

_INSERT = """
INSERT INTO task_journal (task_id, step_id, kind, summary, learnings)
VALUES ($1, $2, $3, $4, $5)
ON CONFLICT (task_id, step_id) WHERE kind = 'step_accepted' DO NOTHING
"""


async def insert(conn: asyncpg.Connection, task_id: str, entries: Sequence[Mapping[str, Any]]) -> None:
    """Add ``entries`` (``step_id``, ``kind``, ``summary``, ``learnings``) on ``conn``, in the caller's
    transaction; a step accepted before keeps its first entry."""
    if entries:
        await conn.executemany(_INSERT, [
            (task_id, entry["step_id"], entry["kind"], entry.get("summary") or "", list(entry.get("learnings") or []))
            for entry in entries
        ])


async def list_entries(
    task_id: str, *, pool: asyncpg.Pool | asyncpg.Connection | None = None,
) -> list[dict[str, Any]]:
    """The task's entries, oldest first, each with its step's label (``step_key``, '' when the step is gone)."""
    pool = pool or get_pool()
    rows = await pool.fetch(
        """
        SELECT j.id, j.task_id, j.step_id, COALESCE(s.step_key, '') AS step_key, j.kind, j.summary, j.learnings,
               j.created_at
        FROM task_journal j LEFT JOIN task_steps s ON s.id = j.step_id
        WHERE j.task_id = $1 ORDER BY j.id
        """,
        task_id,
    )
    return [{**dict(row), "learnings": list(row["learnings"] or [])} for row in rows]
