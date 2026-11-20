"""Durable channel deliveries. A chat's oldest active row always goes first."""
from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import datetime, timedelta
from enum import Enum
from typing import Any

import asyncpg

from .connection import get_pool


class Kind(str, Enum):
    SEND = "send"
    REPLY = "reply"


class Source(str, Enum):
    ANSWER = "answer"
    NOTICE = "notice"
    PROGRESS = "progress"
    POST = "post"
    NOTIFY = "notify"


class State(str, Enum):
    PENDING = "pending"
    SENDING = "sending"
    SENT = "sent"
    FAILED = "failed"
    UNCERTAIN = "uncertain"


def _row(record) -> dict[str, Any] | None:
    if record is None:
        return None
    result = dict(record)
    result["body"] = json.loads(result["body"])
    return result


async def insert_or_get(row: Mapping[str, Any], *, pool: asyncpg.Pool | None = None) -> dict[str, Any]:
    pool = pool or get_pool()
    # Separate statements ensure a conflicting insert committed while we waited
    # is visible to the SELECT at READ COMMITTED.
    await pool.execute("""
        INSERT INTO channel_outbox (channel_id, idempotency_key, kind, chat, reply_to, body, source)
        VALUES ($1,$2,$3,$4,$5,$6::jsonb,$7)
        ON CONFLICT (channel_id, idempotency_key) DO NOTHING
        """, row["channel_id"], row["idempotency_key"], Kind(row["kind"]).value, row["chat"],
        row.get("reply_to"), json.dumps(row["body"]), Source(row["source"]).value)
    result = _row(await pool.fetchrow("SELECT * FROM channel_outbox WHERE channel_id=$1 AND idempotency_key=$2",
                                     row["channel_id"], row["idempotency_key"]))
    assert result is not None
    return result


async def get(id: int, *, pool: asyncpg.Pool | None = None) -> dict[str, Any] | None:
    return _row(await (pool or get_pool()).fetchrow("SELECT * FROM channel_outbox WHERE id=$1", id))


async def next_due(channel_id: str, now: datetime, *, pool: asyncpg.Pool | None = None) -> dict[str, Any] | None:
    return _row(await (pool or get_pool()).fetchrow("""
        SELECT o.* FROM channel_outbox o
        WHERE o.channel_id=$1 AND o.state='pending' AND o.next_attempt_at <= $2
          AND NOT EXISTS (SELECT 1 FROM channel_outbox prior
              WHERE prior.channel_id=o.channel_id AND prior.chat=o.chat
                AND (prior.state='sending' OR (prior.state='pending' AND prior.id < o.id)))
        ORDER BY o.id LIMIT 1
        """, channel_id, now))


async def mark_sending(id: int, *, pool: asyncpg.Pool | None = None) -> bool:
    return await (pool or get_pool()).fetchval("""
        UPDATE channel_outbox o SET state='sending', attempts=attempts+1
        WHERE o.id=$1 AND o.state='pending'
          AND NOT EXISTS (SELECT 1 FROM channel_outbox prior
              WHERE prior.channel_id=o.channel_id AND prior.chat=o.chat AND prior.id<>o.id
                AND (prior.state='sending' OR (prior.state='pending' AND prior.id<o.id)))
        RETURNING TRUE
        """, id) is True


async def mark_sent(id: int, external_id: str | None, *, pool: asyncpg.Pool | None = None) -> None:
    await (pool or get_pool()).execute("UPDATE channel_outbox SET state='sent', external_id=$2, sent_at=now(), "
                                      "error_class=NULL, error=NULL WHERE id=$1", id, external_id)


async def retry_later(id: int, at: datetime, error_class: str, error: str, *, pool: asyncpg.Pool | None = None) -> None:
    await (pool or get_pool()).execute("UPDATE channel_outbox SET state='pending', next_attempt_at=$2, "
        "error_class=$3, error=$4, sent_at=NULL WHERE id=$1", id, at, error_class, error[:500])


async def mark_failed(id: int, error_class: str, error: str, *, pool: asyncpg.Pool | None = None) -> None:
    await (pool or get_pool()).execute("UPDATE channel_outbox SET state='failed', error_class=$2, error=$3, "
                                      "sent_at=NULL WHERE id=$1", id, error_class, error[:500])


async def mark_uncertain(id: int, error: str, *, pool: asyncpg.Pool | None = None) -> None:
    await (pool or get_pool()).execute("UPDATE channel_outbox SET state='uncertain', error_class='unknown', "
                                      "error=$2, sent_at=NULL WHERE id=$1", id, error[:500])


async def recover(channel_id: str, now: datetime, keyed_window: float, *, pool: asyncpg.Pool | None = None) -> None:
    await (pool or get_pool()).execute("""
        UPDATE channel_outbox SET
          state=CASE WHEN $4 AND created_at >= $3 THEN 'pending' ELSE 'uncertain' END,
          next_attempt_at=$2,
          error_class='unknown', error='interrupted while sending'
        WHERE channel_id=$1 AND state='sending'
        """, channel_id, now, now-timedelta(seconds=keyed_window), keyed_window > 0)


async def counts(channel_id: str, *, pool: asyncpg.Pool | None = None) -> dict[str, int]:
    result = {state.value: 0 for state in State}
    for row in await (pool or get_pool()).fetch(
            "SELECT state, count(*) AS n FROM channel_outbox WHERE channel_id=$1 GROUP BY state", channel_id):
        result[row["state"]] = row["n"]
    return result


async def purge(now: datetime, *, pool: asyncpg.Pool | None = None) -> int:
    status = await (pool or get_pool()).execute("""
        DELETE FROM channel_outbox WHERE (state='sent' AND sent_at < $1)
          OR (state<>'sent' AND created_at < $2)
        """, now-timedelta(days=7), now-timedelta(days=30))
    return int(status.split()[-1])


async def defer_unsent(id: int, at: datetime, *, pool: asyncpg.Pool | None = None) -> None:
    """Defer an unsent row, releasing its claim without consuming a retry."""
    await (pool or get_pool()).execute("""
        UPDATE channel_outbox SET state='pending', next_attempt_at=$2,
          attempts=CASE WHEN state='sending' THEN greatest(attempts-1, 0) ELSE attempts END
        WHERE id=$1 AND state IN ('pending', 'sending')
        """, id, at)
