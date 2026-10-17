"""A task's journal (``task_journal``): what happened in the task, written by code as it happens.

``plans.write`` adds entries in the transaction of the change they record (a
step's with the step, an accepted step's in the one that settles it); the
Planner reads them back with its plan (``plans.load``) and the task page with
the task (``tasks.get_task_with_steps``). An entry of the whole task has the
step id ''. The kinds and what each holds are ``agents.tiers.journal``'s.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

import asyncpg

from .connection import get_pool

# Every kind an entry can have (task_journal.kind; agents.tiers.journal.JournalKind).
KINDS = ("step_accepted", "step_failed", "step_retry", "decision", "supplement", "session_switch", "handoff",
         "progress", "profile_choice", "concurrency_change", "role_defined", "gate_opened", "gate_updated",
         "gate_cancelled", "gate_closed")

_INSERT = """
INSERT INTO task_journal (task_id, step_id, kind, summary, learnings, via, delivered_at)
VALUES ($1, $2, $3, $4, $5, $6, CASE WHEN $7 THEN now() END)
ON CONFLICT (task_id, step_id) WHERE kind = 'step_accepted' DO NOTHING
"""


async def insert(conn: asyncpg.Connection, task_id: str, entries: Sequence[Mapping[str, Any]]) -> None:
    """Add ``entries`` (``step_id``, '' for the whole task; ``kind``, ``summary``, ``learnings``; a supplement
    written with its delivery, such as a user gate's conclusion, also ``via`` and ``delivered``) on ``conn``, in
    the caller's transaction, in their order; a step accepted before keeps its first ``step_accepted`` entry."""
    if entries:
        await conn.executemany(_INSERT, [
            (task_id, entry["step_id"], entry["kind"], entry.get("summary") or "", list(entry.get("learnings") or []),
             entry.get("via") or "", bool(entry.get("delivered")))
            for entry in entries
        ])


async def list_entries(
    task_id: str, *, pool: asyncpg.Pool | asyncpg.Connection | None = None,
) -> list[dict[str, Any]]:
    """The task's entries, oldest first, each with its step's label (``step_key``; '' for an entry of the whole
    task or when the step is gone)."""
    pool = pool or get_pool()
    rows = await pool.fetch(
        """
        SELECT j.id, j.task_id, j.step_id, COALESCE(s.step_key, '') AS step_key, j.kind, j.summary, j.learnings,
               j.via, j.delivered_at, j.created_at
        FROM task_journal j LEFT JOIN task_steps s ON s.id = j.step_id
        WHERE j.task_id = $1 ORDER BY j.id
        """,
        task_id,
    )
    return [{**dict(row), "learnings": list(row["learnings"] or [])} for row in rows]
