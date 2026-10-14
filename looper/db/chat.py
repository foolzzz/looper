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
    pool: asyncpg.Pool | None = None,
) -> None:
    pool = pool or get_pool()
    await pool.execute(
        """
        INSERT INTO chat_messages (session_id, message_id, role, content, created_at)
        VALUES ($1, $2, $3, $4, $5)
        ON CONFLICT (session_id, message_id) DO UPDATE
        SET content = EXCLUDED.content
        """,
        session_id, message_id, role, content,
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
        SELECT message_id, role, content, created_at
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
        SELECT session_id, count(*) as message_count,
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
