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
    delivery_failure: str | None = None,
    delivery_error: str | None = None,
    pool: asyncpg.Pool | None = None,
) -> None:
    """Store a message; a Telegram or Lark answer its channel could not send with why (``delivery_failure``, a
    ``channels.base.DeliveryFailure``) and the channel's error."""
    pool = pool or get_pool()
    await pool.execute(
        """
        INSERT INTO chat_messages (session_id, message_id, role, content, channel, created_at, delivery_failure,
                                   delivery_error)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
        ON CONFLICT (session_id, message_id) DO UPDATE
        SET content = EXCLUDED.content, delivery_failure = EXCLUDED.delivery_failure,
            delivery_error = EXCLUDED.delivery_error
        """,
        session_id, message_id, role, content, channel,
        datetime.now(timezone.utc), delivery_failure, delivery_error,
    )


async def list_messages(
    session_id: str,
    *,
    limit: int = 200,
    pool: asyncpg.Pool | None = None,
) -> list[dict[str, Any]]:
    """The session's newest ``limit`` messages, oldest first: a long conversation shows how it stands now. An
    answer its channel could not send has its ``delivery_failure`` and ``delivery_error``."""
    pool = pool or get_pool()
    rows = await pool.fetch(
        """
        SELECT message_id, role, content, channel, created_at, delivery_failure, delivery_error FROM (
            SELECT id, message_id, role, content, channel, created_at, delivery_failure, delivery_error
            FROM chat_messages
            WHERE session_id = $1
            ORDER BY id DESC
            LIMIT $2
        ) newest
        ORDER BY id ASC
        """,
        session_id, limit,
    )
    return [dict(r) for r in rows]


# The web chat keeps each task's Planner thread under that Planner's agent id, ``planner-<task_id>``
# (web/src/pages/chat.tsx ``sessionFor``); every other chat session is a conversation with the Manager.
PLANNER_THREAD_PREFIX = "planner-"
# What a session's listing keeps of its last message.
LAST_MESSAGE_MAX_CHARS = 200


async def list_sessions(
    *,
    limit: int = 50,
    pool: asyncpg.Pool | None = None,
) -> list[dict[str, Any]]:
    """The Manager's conversations on every channel, the most recently active first: each session's channel,
    message count, first and last message times, and its last message's role and start (``last_role``,
    ``last_content``, up to ``LAST_MESSAGE_MAX_CHARS``). Planner threads are left out."""
    pool = pool or get_pool()
    rows = await pool.fetch(
        """
        SELECT s.session_id, s.channel, s.message_count, s.first_message_at, s.last_message_at,
               last.role AS last_role, left(last.content, $3) AS last_content
        FROM (
            SELECT session_id, min(channel) AS channel, count(*) AS message_count,
                   min(created_at) AS first_message_at, max(created_at) AS last_message_at, max(id) AS last_id
            FROM chat_messages
            WHERE NOT starts_with(session_id, $2)
            GROUP BY session_id
            ORDER BY max(created_at) DESC, max(id) DESC
            LIMIT $1
        ) s
        JOIN chat_messages last ON last.id = s.last_id
        ORDER BY s.last_message_at DESC, s.last_id DESC
        """,
        limit, PLANNER_THREAD_PREFIX, LAST_MESSAGE_MAX_CHARS,
    )
    return [dict(r) for r in rows]


async def recent_conversations(
    *,
    sessions: int,
    messages: int,
    pool: asyncpg.Pool | None = None,
) -> list[dict[str, Any]]:
    """The Manager's most recently active chat sessions, newest first, each with its last ``messages``
    messages, oldest first: ``[{"session_id", "messages": [{"role", "content", "created_at", "delivery_failure"}]}]``
    (``delivery_failure`` set for an answer its channel could not send)."""
    pool = pool or get_pool()
    rows = await pool.fetch(
        """
        SELECT r.session_id, m.role, m.content, m.created_at, m.delivery_failure
        FROM (
            SELECT session_id, max(id) AS last_id FROM chat_messages
            WHERE NOT starts_with(session_id, $3)
            GROUP BY session_id ORDER BY max(id) DESC LIMIT $1
        ) r
        CROSS JOIN LATERAL (
            SELECT c.id, c.role, c.content, c.created_at, c.delivery_failure FROM chat_messages c
            WHERE c.session_id = r.session_id ORDER BY c.id DESC LIMIT $2
        ) m
        ORDER BY r.last_id DESC, m.id ASC
        """,
        sessions, messages, PLANNER_THREAD_PREFIX,
    )
    conversations: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        conversations.setdefault(row["session_id"], []).append(
            {"role": row["role"], "content": row["content"], "created_at": row["created_at"],
             "delivery_failure": row["delivery_failure"]})
    return [{"session_id": session_id, "messages": items} for session_id, items in conversations.items()]
