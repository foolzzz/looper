"""``idle_checks``: one row per idle check of a Manager (``agents.idle_checks``).

A check is ``running`` until its turn ends, then ``sending`` while its delivery is in flight. Which items a
check named is kept in the row, so what was mentioned in the last days is read from the rows themselves.
"""
from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import date, datetime
from typing import Any

import asyncpg

from .connection import get_pool

RETENTION_DAYS = 90
# The end states that mean the operator was told, or was shown the items and the Manager judged them known: a later
# check does not name the same item again.
MENTIONED_STATES = ("silent", "sent", "uncertain")
MENTIONED_DAYS = 7
SETTLED_STATES = ("silent", "sent", "uncertain", "deferred", "failed", "preempted")
IN_FLIGHT_STATES = ("running", "sending")
_SETTLE_FROM: dict[str, tuple[str, ...]] = {
    "sent": ("sending",), "uncertain": ("sending",), "failed": IN_FLIGHT_STATES,
    "silent": ("running",), "deferred": ("running",), "preempted": ("running",),
}
FAILED_REASONS = ("turn_failed", "turn_timeout", "invalid_reply", "send_rejected", "delivery_failed", "restart",
                  "manager_off")

Db = asyncpg.Pool | asyncpg.Connection


async def insert_running(conn: Db, *, manager_id: str, local_date: date, items: Sequence[dict[str, Any]],
                         channel_id: str, chat: str) -> int:
    return await conn.fetchval(
        "INSERT INTO idle_checks (manager_id, local_date, items, channel_id, chat) "
        "VALUES ($1, $2, $3::jsonb, $4, $5) RETURNING id",
        manager_id, local_date, json.dumps(list(items)), channel_id, chat)


async def get(check_id: int, *, pool: Db | None = None) -> dict[str, Any] | None:
    row = await (pool or get_pool()).fetchrow("SELECT * FROM idle_checks WHERE id = $1", check_id)
    return None if row is None else _view(row)


def _view(row: asyncpg.Record) -> dict[str, Any]:
    view = dict(row)
    if isinstance(view["items"], str):
        view["items"] = json.loads(view["items"])
    return view


async def settle(check_id: int, state: str, *, reason: str | None = None, reply: str | None = None,
                 record_id: int | None = None, now: datetime | None = None, pool: Db | None = None) -> bool:
    """End a check through its allowed transition; False if another transition already won.

    Only a rejected send fails from ``sending``; other failures belong to the running turn.
    """
    if state not in SETTLED_STATES or (state == "failed") != (reason is not None):
        raise ValueError("a check ends as one of the end states, and a failed one says why")
    if reason is not None and reason not in FAILED_REASONS:
        raise ValueError(f"not a reason a check fails: {reason!r}")
    from_states = _SETTLE_FROM[state]
    if state == "failed":
        from_states = tuple(s for s in from_states if (s == "sending") == (reason == "send_rejected"))
    return await (pool or get_pool()).fetchval(
        "UPDATE idle_checks SET state = $2, reason = $3, reply = $4, record_id = COALESCE($5, record_id), "
        "finished_at = COALESCE($6, now()) WHERE id = $1 AND state = ANY($7::text[]) RETURNING true",
        check_id, state, reason, reply, record_id, now, list(from_states)) is True


async def claim_sending(check_id: int, *, pool: Db | None = None) -> bool:
    """Claim a send once, after its turn has finished and immediately before posting."""
    return await (pool or get_pool()).fetchval(
        "UPDATE idle_checks SET state = 'sending', finished_at = now() "
        "WHERE id = $1 AND state = 'running' RETURNING true", check_id) is True


async def count_on(conn: Db, manager_id: str, local_date: date) -> int:
    """How many checks began on ``local_date``, whatever came of them."""
    return await conn.fetchval("SELECT count(*) FROM idle_checks WHERE manager_id = $1 AND local_date = $2",
                               manager_id, local_date)


async def last_started_at(conn: Db, manager_id: str) -> datetime | None:
    return await conn.fetchval("SELECT max(created_at) FROM idle_checks WHERE manager_id = $1", manager_id)


async def mentioned_items(conn: Db, manager_id: str, since: datetime) -> list[dict[str, Any]]:
    """The items of the checks since ``since`` that ended in a ``MENTIONED_STATES`` state."""
    rows = await conn.fetch("SELECT items FROM idle_checks WHERE manager_id = $1 AND created_at >= $2 "
                            "AND state = ANY($3::text[])", manager_id, since, list(MENTIONED_STATES))
    found: list[dict[str, Any]] = []
    for row in rows:
        items = json.loads(row["items"]) if isinstance(row["items"], str) else row["items"]
        found.extend(items)
    return found


async def settle_running(reason: str, *, manager_id: str | None = None, pool: Db | None = None) -> int:
    """End every ``running`` check (of ``manager_id``, or of all Managers) as failed for ``reason``."""
    if reason not in FAILED_REASONS:
        raise ValueError(f"not a reason a check fails: {reason!r}")
    rows = await (pool or get_pool()).fetch(
        "UPDATE idle_checks SET state = 'failed', reason = $1, finished_at = now() "
        "WHERE state = 'running' AND ($2::text IS NULL OR manager_id = $2) RETURNING id", reason, manager_id)
    return len(rows)


async def purge(before: datetime) -> int:
    result = await get_pool().execute(
        "DELETE FROM idle_checks WHERE id IN (SELECT id FROM idle_checks WHERE created_at < $1 "
        "AND state <> ALL($2::text[]) LIMIT 5000)", before, list(IN_FLIGHT_STATES))
    return int(result.split()[-1])


async def last_states(manager_id: str, limit: int) -> list[str]:
    """The end states of the newest settled checks, newest first."""
    rows = await get_pool().fetch("SELECT state FROM idle_checks WHERE manager_id = $1 AND state <> ALL($3::text[]) "
                                  "ORDER BY created_at DESC, id DESC LIMIT $2", manager_id, limit,
                                  list(IN_FLIGHT_STATES))
    return [row["state"] for row in rows]


async def recover_sending(*, manager_id: str | None = None, pool: Db | None = None) -> list[dict[str, Any]]:
    """An interrupted post may have reached its chat; mark it uncertain without sending again."""
    rows = await (pool or get_pool()).fetch(
        "UPDATE idle_checks SET state = 'uncertain', finished_at = now() "
        "WHERE state = 'sending' AND ($1::text IS NULL OR manager_id = $1) "
        "RETURNING id, manager_id, channel_id, chat", manager_id)
    return [dict(row) for row in rows]
