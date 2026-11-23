"""Durable channel deliveries. A chat's oldest active row always goes first."""
from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import datetime, timedelta
from enum import Enum
from typing import Any
from uuid import UUID

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


async def replace_rejected_body(id: int, body: Mapping[str, Any], *, pool: asyncpg.Pool | None = None) -> dict[str, Any]:
    """Replace one explicitly refused payload; possible or completed delivery is immutable."""
    pool = pool or get_pool()
    await pool.execute("""
        UPDATE channel_outbox SET body=$2::jsonb, state='pending', next_attempt_at=now(),
          error_class=NULL, error=NULL, first_possible_at=NULL, claim_token=NULL
        WHERE id=$1 AND state='failed' AND error_class='rejected' AND body<>$2::jsonb
        """, id, json.dumps(body))
    row = await get(id, pool=pool)
    assert row is not None
    return row


async def next_due(channel_id: str, now: datetime, *, expired_before: datetime | None = None, pool: asyncpg.Pool | None = None) -> dict[str, Any] | None:
    return _row(await (pool or get_pool()).fetchrow("""
        SELECT o.* FROM channel_outbox o
        WHERE o.channel_id=$1 AND o.state='pending'
          AND (o.next_attempt_at <= $2 OR o.created_at <= $3)
          AND NOT EXISTS (SELECT 1 FROM channel_outbox prior
              WHERE prior.channel_id=o.channel_id AND prior.chat=o.chat
                AND (prior.state='sending' OR (prior.state='pending' AND prior.id < o.id)))
        ORDER BY o.id LIMIT 1
        """, channel_id, now, expired_before))


async def mark_sending(id: int, *, claim_token: UUID | None = None, pool: asyncpg.Pool | None = None) -> bool:
    return await (pool or get_pool()).fetchval("""
        UPDATE channel_outbox o SET state='sending',
          attempts=attempts+CASE WHEN state='pending' THEN 1 ELSE 0 END, claim_token=$2
        WHERE o.id=$1 AND (o.state='pending' OR
          (o.state='sending' AND $2::uuid IS NOT NULL AND o.claim_token=$2))
          AND NOT EXISTS (SELECT 1 FROM channel_outbox prior
              WHERE prior.channel_id=o.channel_id AND prior.chat=o.chat AND prior.id<>o.id
                AND (prior.state='sending' OR (prior.state='pending' AND prior.id<o.id)))
        RETURNING TRUE
        """, id, claim_token) is True


async def mark_sent(id: int, external_id: str | None, *, pool: asyncpg.Pool | None = None) -> None:
    await (pool or get_pool()).execute("UPDATE channel_outbox SET state='sent', external_id=$2, sent_at=now(), "
                                      "error_class=NULL, error=NULL WHERE id=$1", id, external_id)


async def retry_later(id: int, at: datetime, error_class: str, error: str, *, clear_possible: bool = False, pool: asyncpg.Pool | None = None) -> None:
    await (pool or get_pool()).execute("UPDATE channel_outbox SET state='pending', next_attempt_at=$2, "
        "error_class=$3, error=$4, sent_at=NULL, "
        "first_possible_at=CASE WHEN $5 THEN NULL ELSE first_possible_at END WHERE id=$1",
        id, at, error_class, error[:500], clear_possible)


async def mark_failed(id: int, error_class: str, error: str, *, pool: asyncpg.Pool | None = None) -> None:
    await (pool or get_pool()).execute("UPDATE channel_outbox SET state='failed', error_class=$2, error=$3, "
                                      "sent_at=NULL, terminal_at=now() WHERE id=$1", id, error_class, error[:500])


async def mark_uncertain(id: int, error: str, *, pool: asyncpg.Pool | None = None) -> None:
    await (pool or get_pool()).execute("UPDATE channel_outbox SET state='uncertain', error_class='unknown', "
                                      "error=$2, sent_at=NULL, terminal_at=now() WHERE id=$1", id, error[:500])


async def mark_attempted(id: int, now: datetime, *, pool: asyncpg.Pool | None = None) -> datetime:
    """Persist the start of the platform dedupe window before any possible delivery."""
    return await (pool or get_pool()).fetchval("""
        UPDATE channel_outbox SET first_possible_at=coalesce(first_possible_at, $2)
        WHERE id=$1 AND state='sending' RETURNING first_possible_at
        """, id, now)


async def recover(channel_id: str, now: datetime, keyed_window: float, *, pool: asyncpg.Pool | None = None) -> None:
    await (pool or get_pool()).execute("""
        UPDATE channel_outbox SET
          state=CASE WHEN $4 AND (first_possible_at IS NULL OR first_possible_at > $3)
                     THEN 'pending' ELSE 'uncertain' END,
          next_attempt_at=$2, claim_token=NULL,
          terminal_at=CASE WHEN $4 AND (first_possible_at IS NULL OR first_possible_at > $3)
                           THEN NULL ELSE $2::timestamptz END,
          error_class='unknown', error='interrupted while sending'
        WHERE channel_id=$1 AND (state='sending' OR
            (state='pending' AND first_possible_at IS NOT NULL AND (NOT $4 OR first_possible_at <= $3)))
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


async def has_unfinished(channel_id: str, *, pool: asyncpg.Pool | None = None) -> bool:
    return await (pool or get_pool()).fetchval(
        "SELECT EXISTS(SELECT 1 FROM channel_outbox WHERE channel_id=$1 AND state IN ('pending','sending'))",
        channel_id)


async def next_attempt(channel_id: str, *, delivery_deadline: float | None = None, pool: asyncpg.Pool | None = None) -> datetime | None:
    """Earliest unblocked chat head; completion wakes heads blocked by in-flight calls."""
    return await (pool or get_pool()).fetchval("""
        SELECT min(least(o.next_attempt_at, o.created_at + $2 * interval '1 second')) FROM channel_outbox o
        WHERE o.channel_id=$1 AND o.state='pending'
          AND NOT EXISTS (SELECT 1 FROM channel_outbox prior
              WHERE prior.channel_id=o.channel_id AND prior.chat=o.chat
                AND (prior.state='sending' OR (prior.state='pending' AND prior.id < o.id)))
        """, channel_id, delivery_deadline)


async def metric_counts(since: datetime, *, pool: asyncpg.Pool | None = None) -> dict[str, dict[str, Any]]:
    """Current backlog (of any age), and failures/uncertainties reached in the window.

    Legacy rows without a terminal timestamp retain their creation-time window.
    """
    result: dict[str, dict[str, Any]] = {}
    for row in await (pool or get_pool()).fetch("""
        SELECT channel_id, state, error_class, count(*) AS n FROM channel_outbox
        WHERE state IN ('pending', 'sending')
          OR (state IN ('failed', 'uncertain') AND coalesce(terminal_at, created_at) >= $1)
        GROUP BY channel_id, state, error_class
        """, since):
        counts = result.setdefault(row["channel_id"], {"queued": 0, "failed_24h": {}, "uncertain_24h": 0})
        if row["state"] in (State.PENDING, State.SENDING):
            counts["queued"] += row["n"]
        elif row["state"] == State.FAILED:
            # Historical rows may have no classification; preserve them as unknown failures.
            key = row["error_class"] or "unknown"
            counts["failed_24h"][key] = counts["failed_24h"].get(key, 0) + row["n"]
        else:
            counts["uncertain_24h"] += row["n"]
    return result
