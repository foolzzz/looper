"""The reads of the "needs you" summary (``agents.attention``) that no other module has: pending intents, the
ledger's own items that wait for the user, and deliveries that may not have arrived. Read only."""

from __future__ import annotations

from datetime import datetime
from typing import Any

import asyncpg

from .connection import get_pool

Db = asyncpg.Pool | asyncpg.Connection


async def pending_intents(*, db: Db | None = None) -> list[dict[str, Any]]:
    """The intents still waiting to be built or rejected, urgent types first, then the newest."""
    rows = await (db or get_pool()).fetch(
        """
        SELECT i.id, i.title, i.confidence, i.created_at, i.expires_at, v.name AS type_name, v.priority
        FROM intents i JOIN intent_type_versions v ON v.type_key = i.type_key AND v.version = i.type_version
        WHERE i.state = 'pending'
        ORDER BY (v.priority = 'urgent') DESC, i.created_at DESC, i.id DESC
        """)
    return [dict(row) for row in rows]


async def ledger_waiting(*, db: Db | None = None) -> list[dict[str, Any]]:
    """The ledger's own open items that wait for the user: a to-do of the user's, a decision the user has not
    confirmed (a relayed one), and anything of the user's past its due time; the overdue first. Sourced items are
    left out: their gates and confirmations are listed from their sources."""
    rows = await (db or get_pool()).fetch(
        """
        SELECT id, kind, title, owner_kind, due_at, created_at, COALESCE(due_at <= now(), false) AS overdue
        FROM ledger_items
        WHERE source_kind IS NULL AND status = 'open'
          AND ((kind = 'pending' AND owner_kind = 'user') OR (kind = 'decision' AND confirmed_event_id IS NULL)
               OR (owner_kind = 'user' AND due_at <= now()))
        ORDER BY overdue DESC, due_at NULLS LAST, id
        """)
    return [dict(row) for row in rows]


async def uncertain_deliveries(since: datetime, *, db: Db | None = None) -> list[dict[str, Any]]:
    """Messages Looper may or may not have sent since ``since`` (``uncertain``: the send was under way when the
    process died, or the channel's answer was lost; never sent again): a subscription's push (one row per run), a
    message about an intent, and the message telling a task's end. Newest first."""
    rows = await (db or get_pool()).fetch(
        """
        SELECT * FROM (
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
        ) AS found
        ORDER BY at DESC, source, key
        """, since)
    return [dict(row) for row in rows]
