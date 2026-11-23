"""Reviews of outbound actions (``outbound_reviews``, the outbound-review RFC, section 9): one row per review, written
before the action may go ahead. ``agents/outbound.py`` writes them; the values are its typed states."""

from __future__ import annotations

from collections.abc import Sequence

import asyncpg

from .connection import get_pool


async def insert(*, agent_id: str, source: str, tool: str, level: str, target_kind: str, target: str, summary: str,
                 verdict: str, reason: str, signals: Sequence[str] = (), failure: str | None = None,
                 action: str | None = None, parent_record_id: int | None = None, record_id: int | None = None,
                 task_id: str | None = None, manager_action_id: int | None = None, channel: str | None = None,
                 chat: str | None = None, profile_id: str | None = None, model: str | None = None,
                 cost_usd: float | None = None, latency_ms: int | None = None,
                 pool: asyncpg.Pool | None = None) -> int:
    """The new review's id."""
    pool = pool or get_pool()
    return await pool.fetchval(
        "INSERT INTO outbound_reviews (agent_id, parent_record_id, record_id, task_id, manager_action_id, channel, "
        "chat, source, tool, action, level, target_kind, target, summary, verdict, failure, reason, signals, "
        "profile_id, model, cost_usd, latency_ms) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, "
        "$14, $15, $16, $17, $18::text[], $19, $20, $21, $22) RETURNING id",
        agent_id, parent_record_id, record_id, task_id, manager_action_id, channel, chat, source, tool, action, level,
        target_kind, target, summary, verdict, failure, reason, list(signals), profile_id, model, cost_usd,
        latency_ms)


async def link_action(review_id: int, manager_action_id: int, *, pool: asyncpg.Pool | None = None) -> None:
    """Name the ``manager_actions`` row of the call ``review_id`` reviewed (written after the review)."""
    pool = pool or get_pool()
    await pool.execute("UPDATE outbound_reviews SET manager_action_id = $2 WHERE id = $1", review_id,
                       manager_action_id)
