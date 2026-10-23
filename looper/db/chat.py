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
    session_id: str | None = None,
    manager_id: str = "manager",
    pool: asyncpg.Pool | None = None,
) -> list[dict[str, Any]]:
    """Manager ``manager_id``'s (default: the default Manager's) conversations on every channel, the most recently active first: each session's channel,
    message count, first and last message times, and its last message's role and start (``last_role``,
    ``last_content``, up to ``LAST_MESSAGE_MAX_CHARS``). Planner threads are left out. With ``session_id``, only
    that session, however long ago it was active (none when it has no messages). A session without a
    ``chat_threads`` row is the default Manager's; a deleted Manager's threads keep their row, so they are in no list."""
    pool = pool or get_pool()
    rows = await pool.fetch(
        """
        SELECT s.session_id, s.channel, s.message_count, s.first_message_at, s.last_message_at,
               last.role AS last_role, left(last.content, $3) AS last_content
        FROM (
            SELECT session_id, min(m.channel) AS channel, count(*) AS message_count,
                   min(m.created_at) AS first_message_at, max(m.created_at) AS last_message_at, max(m.id) AS last_id
            FROM chat_messages m
            LEFT JOIN chat_threads t USING (session_id)
            WHERE NOT starts_with(session_id, $2) AND ($4::text IS NULL OR session_id = $4)
              AND coalesce(t.manager_id, 'manager') = $5
            GROUP BY session_id
            ORDER BY max(m.created_at) DESC, max(m.id) DESC
            LIMIT $1
        ) s
        JOIN chat_messages last ON last.id = s.last_id
        ORDER BY s.last_message_at DESC, s.last_id DESC
        """,
        limit, PLANNER_THREAD_PREFIX, LAST_MESSAGE_MAX_CHARS, session_id, manager_id,
    )
    return [dict(r) for r in rows]


async def has_messages(session_id: str, *, pool: asyncpg.Pool | asyncpg.Connection | None = None) -> bool:
    pool = pool or get_pool()
    return bool(await pool.fetchval("SELECT EXISTS (SELECT 1 FROM chat_messages WHERE session_id = $1)", session_id))


async def recent_conversations(
    *,
    sessions: int,
    messages: int,
    manager_id: str = "manager",
    pool: asyncpg.Pool | None = None,
) -> list[dict[str, Any]]:
    """Manager ``manager_id``'s (default: the default Manager's; a thread without a ``chat_threads`` row is the
    default's) most recently active chat sessions, newest first, each with its last ``messages``
    messages, oldest first: ``[{"session_id", "messages": [{"role", "content", "created_at", "delivery_failure"}]}]``
    (``delivery_failure`` set for an answer its channel could not send)."""
    pool = pool or get_pool()
    rows = await pool.fetch(
        """
        SELECT r.session_id, m.role, m.content, m.created_at, m.delivery_failure
        FROM (
            SELECT c.session_id, max(c.id) AS last_id FROM chat_messages c
            LEFT JOIN chat_threads t ON t.session_id = c.session_id
            WHERE NOT starts_with(c.session_id, $3) AND coalesce(t.manager_id, 'manager') = $4
            GROUP BY c.session_id ORDER BY max(c.id) DESC LIMIT $1
        ) r
        CROSS JOIN LATERAL (
            SELECT c.id, c.role, c.content, c.created_at, c.delivery_failure FROM chat_messages c
            WHERE c.session_id = r.session_id ORDER BY c.id DESC LIMIT $2
        ) m
        ORDER BY r.last_id DESC, m.id ASC
        """,
        sessions, messages, PLANNER_THREAD_PREFIX, manager_id,
    )
    conversations: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        conversations.setdefault(row["session_id"], []).append(
            {"role": row["role"], "content": row["content"], "created_at": row["created_at"],
             "delivery_failure": row["delivery_failure"]})
    return [{"session_id": session_id, "messages": items} for session_id, items in conversations.items()]


async def thread_owner(session_id: str, *, pool: asyncpg.Pool | asyncpg.Connection | None = None) -> str | None:
    """The Manager a chat thread belongs to; None for a thread without a row (the default Manager's)."""
    pool = pool or get_pool()
    return await pool.fetchval("SELECT manager_id FROM chat_threads WHERE session_id = $1", session_id)


async def claim_thread(session_id: str, manager_id: str, *, client_id: str | None = None,
                       pool: asyncpg.Pool | asyncpg.Connection | None = None) -> str:
    """Make ``manager_id`` the owner of the thread unless it has one, and note the API client it came through
    (``client_id``); the stored owner (the first claim wins)."""
    pool = pool or get_pool()
    await pool.execute("INSERT INTO chat_threads (session_id, manager_id, client_id) VALUES ($1, $2, $3) "
                       "ON CONFLICT (session_id) DO NOTHING", session_id, manager_id, client_id)
    return await pool.fetchval("SELECT manager_id FROM chat_threads WHERE session_id = $1", session_id)


async def thread_client(session_id: str, *, pool: asyncpg.Pool | asyncpg.Connection | None = None) -> str | None:
    """The API client a chat thread came through; None for a thread without one."""
    pool = pool or get_pool()
    return await pool.fetchval("SELECT client_id FROM chat_threads WHERE session_id = $1", session_id)


async def client_threads(client_id: str, *, limit: int = 50,
                         pool: asyncpg.Pool | asyncpg.Connection | None = None) -> list[dict[str, Any]]:
    """The API client's threads, the most recently active first: ``session_id``, ``manager_id``, ``created_at``,
    and the time (``last_message_at``) and start (``last_content``, up to ``LAST_MESSAGE_MAX_CHARS``) of the last
    message, both None for a thread without one yet."""
    pool = pool or get_pool()
    rows = await pool.fetch(
        """
        SELECT t.session_id, t.manager_id, t.created_at, last.created_at AS last_message_at,
               left(last.content, $3) AS last_content
        FROM chat_threads t
        LEFT JOIN LATERAL (
            SELECT m.created_at, m.content FROM chat_messages m WHERE m.session_id = t.session_id
            ORDER BY m.id DESC LIMIT 1
        ) last ON true
        WHERE t.client_id = $1
        ORDER BY last.created_at DESC NULLS LAST, t.created_at DESC
        LIMIT $2
        """,
        client_id, limit, LAST_MESSAGE_MAX_CHARS,
    )
    return [dict(r) for r in rows]


class UnknownCursor(LookupError):
    """``messages_page`` was given an ``after`` that is not a message of the session."""


async def messages_page(session_id: str, *, after: str | None = None, limit: int = 100,
                        pool: asyncpg.Pool | asyncpg.Connection | None = None) -> tuple[list[dict[str, Any]], bool]:
    """Up to ``limit`` (1–200, the caller clamps) messages of the session after message ``after`` (from the
    start without it), in storage order: ``message_id``, ``role``, ``content``, ``created_at``; and whether more
    follow. An ``after`` that is not one of the session's messages raises ``UnknownCursor``."""
    assert 1 <= limit <= 200, limit
    pool = pool or get_pool()
    after_id = 0
    if after is not None:
        after_id = await pool.fetchval(
            "SELECT id FROM chat_messages WHERE session_id = $1 AND message_id = $2", session_id, after)
        if after_id is None:
            raise UnknownCursor(after)
    rows = await pool.fetch(
        "SELECT message_id, role, content, created_at FROM chat_messages WHERE session_id = $1 AND id > $2 "
        "ORDER BY id LIMIT $3", session_id, after_id, limit + 1)
    return [dict(r) for r in rows[:limit]], len(rows) > limit
