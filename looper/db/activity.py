"""Activity (execution trace) read/write operations."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import asyncpg

from .connection import get_pool


async def insert_activity(
    *,
    task_id: str,
    worker: str = "",
    kind: str,
    summary: str | None = None,
    detail: str | None = None,
    metadata: dict[str, Any] | None = None,
    input_tokens: int | None = None,
    output_tokens: int | None = None,
    pool: asyncpg.Pool | None = None,
) -> int:
    """Insert an activity row and return its seq (for SSE cursor)."""
    pool = pool or get_pool()
    import json
    meta_json = json.dumps(metadata) if metadata else None

    seq: int = await pool.fetchval(
        """
        INSERT INTO activity (task_id, worker, kind, summary, detail,
                              metadata, input_tokens, output_tokens, created_at)
        VALUES ($1, $2, $3, $4, $5, $6::jsonb, $7, $8, $9)
        RETURNING seq
        """,
        task_id, worker, kind, summary, detail,
        meta_json, input_tokens, output_tokens,
        datetime.now(timezone.utc),
    )
    return seq


async def list_activity(
    task_id: str,
    *,
    since_seq: int = 0,
    limit: int = 100,
    pool: asyncpg.Pool | None = None,
) -> list[dict[str, Any]]:
    """List activity rows after a cursor, summary only (no detail)."""
    pool = pool or get_pool()
    rows = await pool.fetch(
        """
        SELECT seq, task_id, worker, kind, summary, metadata,
               input_tokens, output_tokens, created_at
        FROM activity
        WHERE task_id = $1 AND seq > $2
        ORDER BY seq ASC
        LIMIT $3
        """,
        task_id, since_seq, limit,
    )
    return [dict(r) for r in rows]


async def get_activity_detail(seq: int, *, pool: asyncpg.Pool | None = None) -> dict[str, Any] | None:
    """Get a single activity row including the detail field."""
    pool = pool or get_pool()
    row = await pool.fetchrow("SELECT * FROM activity WHERE seq = $1", seq)
    if row is None:
        return None
    return dict(row)


WINDOW_LIMIT = 1000
CONVERSATION_KINDS = ("text", "phase", "error", "user_message")


def _window_limit(limit: int) -> int:
    return max(1, min(limit, WINDOW_LIMIT))


async def activity_window(
    task_id: str,
    *,
    before: int | None = None,
    limit: int = WINDOW_LIMIT,
    pool: asyncpg.Pool | None = None,
) -> dict[str, Any]:
    """The latest ``limit`` activity rows of a task (the ones just before seq ``before`` when given), oldest first,
    summary only; ``has_more``: older rows exist; ``oldest_seq``: the window's first seq; ``latest_seq``: the task's
    newest seq (None for a task without activity)."""
    pool = pool or get_pool()
    limit = _window_limit(limit)
    rows = await pool.fetch(
        """
        SELECT seq, task_id, worker, kind, summary, metadata, input_tokens, output_tokens, created_at
        FROM activity
        WHERE task_id = $1 AND ($2::bigint IS NULL OR seq < $2)
        ORDER BY seq DESC
        LIMIT $3
        """,
        task_id, before, limit + 1,
    )
    latest = await pool.fetchval("SELECT max(seq) FROM activity WHERE task_id = $1", task_id)
    return _window(rows, limit, latest)


def _window(rows: list[Any], limit: int, latest: int | None) -> dict[str, Any]:
    has_more = len(rows) > limit
    items = [dict(r) for r in reversed(rows[:limit])]
    return {"rows": items, "has_more": has_more, "oldest_seq": items[0]["seq"] if items else None,
            "latest_seq": latest}


async def conversation_window(
    task_id: str,
    *,
    before: int | None = None,
    limit: int = WINDOW_LIMIT,
    pool: asyncpg.Pool | None = None,
) -> dict[str, Any]:
    """``activity_window`` of the conversation rows (text, phase, error, user message)."""
    pool = pool or get_pool()
    limit = _window_limit(limit)
    rows = await pool.fetch(
        """
        SELECT seq, worker, kind, summary, created_at
        FROM activity
        WHERE task_id = $1 AND kind = ANY($4::text[]) AND ($2::bigint IS NULL OR seq < $2)
        ORDER BY seq DESC
        LIMIT $3
        """,
        task_id, before, limit + 1, list(CONVERSATION_KINDS),
    )
    latest = await pool.fetchval(
        "SELECT max(seq) FROM activity WHERE task_id = $1 AND kind = ANY($2::text[])",
        task_id, list(CONVERSATION_KINDS))
    return _window(rows, limit, latest)


async def list_conversation(
    task_id: str,
    *,
    pool: asyncpg.Pool | None = None,
) -> list[dict[str, Any]]:
    """Get conversation-relevant activity for a task (text, phase, error)."""
    pool = pool or get_pool()
    rows = await pool.fetch(
        """
        SELECT seq, worker, kind, summary, created_at
        FROM activity
        WHERE task_id = $1 AND kind IN ('text', 'phase', 'error', 'user_message')
        ORDER BY seq ASC
        """,
        task_id,
    )
    return [dict(r) for r in rows]
