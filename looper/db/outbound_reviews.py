"""Reviews of outbound actions (``outbound_reviews``, the outbound-review RFC, section 9): one row per review, written
before the action may go ahead. ``agents/outbound.py`` writes them; the values are its typed states."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
import math
from typing import Any

import asyncpg

from .connection import get_pool


async def list_reviews(*, verdict: str | None = None, task_id: str | None = None,
                       before: tuple[datetime, int] | None = None, limit: int = 50,
                       pool: asyncpg.Pool | None = None) -> list[dict[str, Any]]:
    """At most ``limit + 1`` audit facts, newest first; never join or return the action's private content."""
    if type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError("limit must be an integer from 1 to 100")
    pool = pool or get_pool()
    values: list[Any] = []
    filters = []
    for column, value in (("verdict", verdict), ("task_id", task_id)):
        if value is not None:
            values.append(value)
            filters.append(f"{column} = ${len(values)}")
    if before is not None:
        values.extend(before)
        filters.append(f"(created_at, id) < (${len(values) - 1}, ${len(values)})")
    values.append(limit + 1)
    where = " WHERE " + " AND ".join(filters) if filters else ""
    rows = await pool.fetch(
        "SELECT id, created_at, agent_id, task_id, parent_record_id, record_id, source, tool, action, level, "
        "target_kind, target, summary, verdict, failure, reason, signals, profile_id, model, cost_usd, latency_ms "
        f"FROM outbound_reviews{where} ORDER BY created_at DESC, id DESC LIMIT ${len(values)}", *values)
    result = []
    for record in rows:
        row = dict(record)
        for key in ("id", "parent_record_id", "record_id"):
            row[key] = str(row[key]) if row[key] is not None else None
        row["created_at"] = row["created_at"].isoformat()
        if row["cost_usd"] is not None:
            row["cost_usd"] = float(row["cost_usd"])
            if not math.isfinite(row["cost_usd"]):
                raise ValueError("review cost is not finite")
        result.append(row)
    return result


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
