"""Chat message persistence."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import asyncpg

from .connection import get_pool


async def save_message(
    *,
    session_id: str,
    message_id: str,
    role: str,
    content: str,
    channel: str = "web",
    pool: asyncpg.Pool | None = None,
) -> None:
    pool = pool or get_pool()
    await pool.execute(
        """
        INSERT INTO chat_messages (session_id, message_id, role, content, channel, created_at)
        VALUES ($1, $2, $3, $4, $5, $6)
        ON CONFLICT (session_id, message_id) DO UPDATE
        SET content = EXCLUDED.content
        """,
        session_id, message_id, role, content, channel,
        datetime.now(timezone.utc),
    )


async def list_messages(
    session_id: str,
    *,
    limit: int = 200,
    pool: asyncpg.Pool | None = None,
) -> list[dict[str, Any]]:
    pool = pool or get_pool()
    rows = await pool.fetch(
        """
        SELECT message_id, role, content, channel, created_at
        FROM chat_messages
        WHERE session_id = $1
        ORDER BY id ASC
        LIMIT $2
        """,
        session_id, limit,
    )
    return [dict(r) for r in rows]


async def list_sessions(
    *,
    limit: int = 50,
    pool: asyncpg.Pool | None = None,
) -> list[dict[str, Any]]:
    """List recent sessions with message count."""
    pool = pool or get_pool()
    rows = await pool.fetch(
        """
        SELECT session_id, min(channel) as channel, count(*) as message_count,
               min(created_at) as first_message_at,
               max(created_at) as last_message_at
        FROM chat_messages
        GROUP BY session_id
        ORDER BY max(created_at) DESC
        LIMIT $1
        """,
        limit,
    )
    return [dict(r) for r in rows]


# The web chat keeps each task's Planner thread under that Planner's agent id, ``planner-<task_id>``
# (web/src/pages/chat.tsx ``sessionFor``); every other chat session is a conversation with the Manager.
PLANNER_THREAD_PREFIX = "planner-"


async def recent_conversations(
    *,
    sessions: int,
    messages: int,
    pool: asyncpg.Pool | None = None,
) -> list[dict[str, Any]]:
    """The Manager's most recently active chat sessions, newest first, each with its last ``messages``
    messages, oldest first: ``[{"session_id", "messages": [{"role", "content", "created_at"}]}]``."""
    pool = pool or get_pool()
    rows = await pool.fetch(
        """
        SELECT r.session_id, m.role, m.content, m.created_at
        FROM (
            SELECT session_id, max(id) AS last_id FROM chat_messages
            WHERE NOT starts_with(session_id, $3)
            GROUP BY session_id ORDER BY max(id) DESC LIMIT $1
        ) r
        CROSS JOIN LATERAL (
            SELECT c.id, c.role, c.content, c.created_at FROM chat_messages c
            WHERE c.session_id = r.session_id ORDER BY c.id DESC LIMIT $2
        ) m
        ORDER BY r.last_id DESC, m.id ASC
        """,
        sessions, messages, PLANNER_THREAD_PREFIX,
    )
    conversations: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        conversations.setdefault(row["session_id"], []).append(
            {"role": row["role"], "content": row["content"], "created_at": row["created_at"]})
    return [{"session_id": session_id, "messages": items} for session_id, items in conversations.items()]
