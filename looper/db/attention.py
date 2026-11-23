"""The reads of the "needs you" summary (``agents.attention``) that no other module has: pending intents, the
ledger's own items that wait for the user, and deliveries that may not have arrived. Read only. Each returns at most
``limit`` rows and how many match in all."""

from __future__ import annotations

from datetime import datetime
from typing import Any

import asyncpg

from .connection import get_pool

Db = asyncpg.Pool | asyncpg.Connection


async def _limited(db: Db | None, sql: str, order: str, limit: int, *args: Any) -> tuple[list[dict[str, Any]], int]:
    """The first ``limit`` rows of ``sql`` in ``order`` (of its columns) and the count of all of them."""
    rows = await (db or get_pool()).fetch(
        f"SELECT *, count(*) OVER () AS matching FROM ({sql}) AS found ORDER BY {order} LIMIT ${len(args) + 1}",
        *args, limit)
    return [{key: row[key] for key in row.keys() if key != "matching"} for row in rows], (
        rows[0]["matching"] if rows else 0)


_PENDING_INTENTS = """
        SELECT i.id, i.title, i.confidence, i.created_at, i.expires_at, v.name AS type_name, v.priority
        FROM intents i JOIN intent_type_versions v ON v.type_key = i.type_key AND v.version = i.type_version
        WHERE i.state = 'pending'
        """


async def pending_intents(limit: int, *, db: Db | None = None) -> tuple[list[dict[str, Any]], int]:
    """The intents still waiting to be built or rejected, urgent types first, then the newest."""
    return await _limited(db, _PENDING_INTENTS, "(priority = 'urgent') DESC, created_at DESC, id DESC", limit)


async def earliest_pending_intent(*, db: Db | None = None) -> dict[str, Any] | None:
    """The oldest intent still waiting, whichever its priority. The capped list above is not chronological."""
    row = await (db or get_pool()).fetchrow(
        f"SELECT * FROM ({_PENDING_INTENTS}) AS found ORDER BY created_at ASC, id ASC LIMIT 1")
    return dict(row) if row is not None else None


_LEDGER_WAITING = """
        SELECT id, kind, title, owner_kind, due_at, created_at, COALESCE(due_at <= now(), false) AS overdue
        FROM ledger_items
        WHERE source_kind IS NULL AND status = 'open'
          AND ((kind = 'pending' AND owner_kind = 'user') OR (kind = 'decision' AND confirmed_event_id IS NULL)
               OR (owner_kind = 'user' AND due_at <= now()))
        """


async def ledger_waiting(limit: int, *, db: Db | None = None) -> tuple[list[dict[str, Any]], int]:
    """The ledger's own open items that wait for the user: a to-do of the user's, a decision the user has not
    confirmed (a relayed one), and anything of the user's past its due time; the overdue first. Sourced items are
    left out: their gates and confirmations are listed from their sources."""
    return await _limited(db, _LEDGER_WAITING, "overdue DESC, due_at NULLS LAST, id", limit)


async def earliest_ledger_item(*, db: Db | None = None) -> dict[str, Any] | None:
    """The oldest ledger item that waits, by when it was created. The capped list above leads with the overdue."""
    row = await (db or get_pool()).fetchrow(
        f"SELECT * FROM ({_LEDGER_WAITING}) AS found ORDER BY created_at ASC, id ASC LIMIT 1")
    return dict(row) if row is not None else None


_UNCERTAIN_DELIVERIES = """
            SELECT 'subscription' AS source, d.run_id AS key, s.id AS ref, s.name AS title,
                   r.target_channel AS channel, max(d.updated_at) AS at, NULL::text AS goal_id,
                   NULL::text AS requirement
            FROM subscription_deliveries d
            JOIN subscription_runs r ON r.run_id = d.run_id
            JOIN schedule_runs sr ON sr.id = d.run_id
            JOIN schedules s ON s.id = sr.schedule_id
            WHERE d.state = 'uncertain' AND d.updated_at >= $1
            GROUP BY d.run_id, s.id, s.name, r.target_channel
            UNION ALL
            SELECT 'intent', p.id::text, p.intent_id::text, i.title, p.channel_id, p.updated_at, NULL, NULL
            FROM intent_posts p JOIN intents i ON i.id = p.intent_id
            WHERE p.state = 'uncertain' AND p.updated_at >= $1
            UNION ALL
            SELECT 'task_end', e.id::text, e.task_id, t.title, e.channel_id, e.created_at, t.goal_id,
                   left(t.requirement, 200)
            FROM manager_events e JOIN tasks t ON t.id = e.task_id
            WHERE e.delivery = 'uncertain' AND e.created_at >= $1
        """


async def uncertain_deliveries(since: datetime, limit: int, *,
                               db: Db | None = None) -> tuple[list[dict[str, Any]], int]:
    """Messages Looper may or may not have sent since ``since`` (``uncertain``: the send was under way when the
    process died, or the channel's answer was lost; never sent again): a subscription's push (one row per run), a
    message about an intent, and the message telling a task's end. Newest first. Each table's partial index of its
    uncertain rows (schema.sql) keeps this off its history."""
    return await _limited(db, _UNCERTAIN_DELIVERIES, "at DESC, source, key", limit, since)


async def earliest_uncertain_delivery(since: datetime, *, db: Db | None = None) -> dict[str, Any] | None:
    """The oldest delivery that may not have arrived since ``since``. The capped list above is newest first."""
    row = await (db or get_pool()).fetchrow(
        f"SELECT * FROM ({_UNCERTAIN_DELIVERIES}) AS found ORDER BY at ASC, source, key LIMIT 1", since)
    return dict(row) if row is not None else None
