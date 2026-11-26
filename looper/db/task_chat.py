"""Rows of one task conversation: the model history, and whether a restart cut a turn."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal

from ..agents.task_commands import history_for_model
from .chat import task_thread
from .connection import get_pool


async def claim_user_message(session_id: str, message_id: str, content: str, *,
                             channel: str = "web",
                             entry_kind: str = "message") -> Literal["inserted", "same", "conflict"]:
    """Insert the user row once. The same text again is ``same``; a different text is ``conflict`` and is not stored.

    ``save_message`` upserts and would replace the first text, so the claim is this insert. The unique
    ``(session_id, message_id)`` is what makes two concurrent retries agree on one row. ``entry_kind`` is stored
    with that first insert; a retry does not change it.
    """
    pool = get_pool()
    async with pool.acquire() as conn, conn.transaction():
        inserted = await conn.fetchrow(
            """
            INSERT INTO chat_messages (session_id, message_id, role, content, channel, created_at, entry_kind)
            VALUES ($1, $2, 'user', $3, $4, $5, $6)
            ON CONFLICT (session_id, message_id) DO NOTHING
            RETURNING content
            """,
            session_id, message_id, content, channel, datetime.now(timezone.utc), entry_kind)
        if inserted is not None:
            return "inserted"
        existing = await conn.fetchval(
            "SELECT content FROM chat_messages WHERE session_id = $1 AND message_id = $2",
            session_id, message_id)
        return "same" if existing == content else "conflict"


async def message_content(session_id: str, message_id: str) -> str | None:
    """The stored text of one row, or None when that id was never claimed."""
    return await get_pool().fetchval(
        "SELECT content FROM chat_messages WHERE session_id = $1 AND message_id = $2",
        session_id, message_id)


async def recent(task_id: str, limit: int) -> list[dict[str, Any]]:
    """The last ``limit`` user and assistant rows a model may see, oldest first.

    Tool and event rows are left out. Rows stored as ``command`` or ``command_reply`` are left out too, so a fresh
    retry after a resume failure does not hand those rows to the model. ``created_at`` is text: a resume stores the
    list as JSON. The read looks past a run of commands so they do not take the model's slots. ``entry_kind`` stays
    on each kept row so a later resume still trusts the stored kind.
    """
    window = min(max(limit, 1) * 5, 200)
    rows = await get_pool().fetch(
        """
        SELECT role, content, created_at, entry_kind FROM (
            SELECT id, role, content, created_at, entry_kind FROM chat_messages
            WHERE session_id = $1 AND role IN ('user', 'assistant')
            ORDER BY id DESC
            LIMIT $2
        ) m
        ORDER BY id ASC
        """,
        task_thread(task_id), window)
    history = []
    for row in rows:
        created = row["created_at"]
        history.append({"role": row["role"], "content": row["content"], "entry_kind": row["entry_kind"],
                        "created_at": created.isoformat() if hasattr(created, "isoformat") else created})
    return history_for_model(history)[-limit:]


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
