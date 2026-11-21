"""The ``proactive_deliveries`` table: one row per proactive message a send point decided on with the delivery layer
on (``agents/proactive_delivery``). A row is keyed by its message (``dedupe_key``), so a message tried again keeps
the decision it got first; ``sent_at`` is when the channel took one that was to be sent."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date
from typing import Any

import asyncpg

from .connection import get_pool

_COLUMNS = ("dedupe_key", "kind", "urgency", "channel_id", "chat", "chat_kind", "task_id", "step_id", "schedule_id",
            "outcome", "reason", "counted", "local_date", "digest_date", "decided_at")


async def lock_chat(conn: asyncpg.Connection, channel_id: str, chat: str) -> None:
    """Serialize the decisions on one chat until this transaction ends, so two send points cannot both take the
    chat's last message of the day."""
    await conn.execute("SELECT pg_advisory_xact_lock(hashtextextended($1, 0))", f"proactive:{channel_id}:{chat}")


async def get(conn: asyncpg.Connection | asyncpg.Pool, dedupe_key: str) -> dict[str, Any] | None:
    row = await conn.fetchrow("SELECT * FROM proactive_deliveries WHERE dedupe_key = $1", dedupe_key)
    return None if row is None else dict(row)


async def counted_on(conn: asyncpg.Connection, channel_id: str, chat: str, local_date: date) -> int:
    """How many counted messages the chat had on ``local_date``."""
    return await conn.fetchval("SELECT count(*) FROM proactive_deliveries WHERE channel_id = $1 AND chat = $2 "
                               "AND local_date = $3 AND counted", channel_id, chat, local_date)


async def insert(conn: asyncpg.Connection, row: Mapping[str, Any]) -> dict[str, Any]:
    """Write the decision; the row already there when another decision on the same message got in first."""
    added = await conn.fetchrow(
        f"INSERT INTO proactive_deliveries ({', '.join(_COLUMNS)}) "
        f"VALUES ({', '.join(f'${n}' for n in range(1, len(_COLUMNS) + 1))}) "
        "ON CONFLICT (dedupe_key) WHERE dedupe_key IS NOT NULL DO NOTHING RETURNING *",
        *(row[name] for name in _COLUMNS))
    return dict(added) if added is not None else await get(conn, row["dedupe_key"])


async def mark_sent(dedupe_key: str, *, pool: asyncpg.Pool | None = None) -> None:
    """Note that the channel took the message (the first time only)."""
    pool = pool or get_pool()
    await pool.execute("UPDATE proactive_deliveries SET sent_at = now() "
                       "WHERE dedupe_key = $1 AND outcome = 'send' AND sent_at IS NULL", dedupe_key)
