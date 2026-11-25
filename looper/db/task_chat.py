"""Rows of one task conversation: the model history, and whether a restart cut a turn."""

from __future__ import annotations

from typing import Any, Literal

from .chat import task_thread
from .connection import get_pool


async def recent(task_id: str, limit: int) -> list[dict[str, Any]]:
    """The last ``limit`` user and assistant rows of the task's thread, oldest first.

    Tool and event rows are left out, so the history a turn resumes from is the conversation. ``created_at`` is
    text: a resume stores the list as JSON.
    """
    rows = await get_pool().fetch(
        """
        SELECT role, content, created_at FROM (
            SELECT id, role, content, created_at FROM chat_messages
            WHERE session_id = $1 AND role IN ('user', 'assistant')
            ORDER BY id DESC
            LIMIT $2
        ) m
        ORDER BY id ASC
        """,
        task_thread(task_id), limit)
    history = []
    for row in rows:
        created = row["created_at"]
        history.append({"role": row["role"], "content": row["content"],
                        "created_at": created.isoformat() if hasattr(created, "isoformat") else created})
    return history


async def turn_state(task_id: str) -> Literal["idle", "running", "interrupted"]:
    """``running`` while the pool is in a turn; ``interrupted`` when the last word is the user's and nothing is.

    A restart drops the in-memory turn and leaves that user row. A tool or event row does not count as a reply.
    """
    from ..agents.tiers.task_manager import current_pool

    pool = current_pool()
    if pool is not None and pool.in_turn(task_id):
        return "running"
    role = await get_pool().fetchval(
        """
        SELECT role FROM chat_messages
        WHERE session_id = $1 AND role IN ('user', 'assistant')
        ORDER BY id DESC
        LIMIT 1
        """,
        task_thread(task_id))
    if role == "user":
        return "interrupted"
    return "idle"
