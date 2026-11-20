"""Completion events: one row per task end that has a chat to tell (manager completion ledger RFC).

The row keeps the code notice the end would send (``fallback``) so that whatever happens to a later Manager turn,
the notice goes out exactly as it always did. ``dedupe_key`` makes a replayed message record once. Every change of
state is one ``UPDATE ... WHERE state = <the old one>``: when two callers move the same row, one of them does.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from collections.abc import Sequence
from datetime import datetime
from typing import Any
from uuid import UUID

import asyncpg

from .connection import get_pool

Db = asyncpg.Pool | asyncpg.Connection

RETENTION_DAYS = 30
# The most events one completion turn tells.
CLAIM_LIMIT = 10
# A Manager that starts sends the notices of fallen-back events this recent that were never sent.
UNSENT_HOURS = 24
PRUNE_BATCH = 5000
SUMMARY_HOURS = 24

_COLUMNS = ("manager_id", "kind", "task_id", "origin", "schedule_run_id", "target_kind", "channel_id", "chat",
            "session_key", "dedupe_key", "payload", "fallback", "state", "reason")


@dataclass(frozen=True)
class Recorded:
    """The event of a ``dedupe_key``; ``inserted`` is whether this call wrote it."""

    row: dict[str, Any]
    inserted: bool

    @property
    def send_fallback(self) -> bool:
        """Whether the caller sends the fallback now: a new row that is not waiting for a turn, or a replayed one
        whose fallback was never handed to the channel."""
        row = self.row
        if row["state"] not in ("skipped", "fallback"):
            return False
        return self.inserted or row["sent_at"] is None


async def record(*, manager_id: str, task_id: str, origin: str, target_kind: str, channel_id: str, chat: str,
                 dedupe_key: str, payload: dict[str, Any], fallback: list[dict[str, Any]], state: str,
                 reason: str | None, schedule_run_id: str | None = None, session_key: str | None = None,
                 kind: str = "task_end", pool: Db | None = None) -> Recorded:
    """Insert the event, or read the one a replay already wrote."""
    db = pool or get_pool()
    values = {"manager_id": manager_id, "kind": kind, "task_id": task_id, "origin": origin,
              "schedule_run_id": schedule_run_id, "target_kind": target_kind, "channel_id": channel_id,
              "chat": chat, "session_key": session_key, "dedupe_key": dedupe_key,
              "payload": json.dumps(payload, ensure_ascii=False), "fallback": json.dumps(fallback, ensure_ascii=False),
              "state": state, "reason": reason}
    marks = ", ".join(f"${n}" + ("::jsonb" if name in ("payload", "fallback") else "")
                      for n, name in enumerate(_COLUMNS, 1))
    # A pending event is due at once (no gathering window yet).
    row = await db.fetchrow(
        f"INSERT INTO manager_events ({', '.join(_COLUMNS)}, due_at) VALUES ({marks}, "
        f"CASE WHEN ${_COLUMNS.index('state') + 1} = 'pending' THEN now() END) "
        "ON CONFLICT (dedupe_key) DO NOTHING RETURNING *", *(values[name] for name in _COLUMNS))
    if row is not None:
        return Recorded(dict(row), True)
    existing = await db.fetchrow("SELECT * FROM manager_events WHERE dedupe_key = $1", dedupe_key)
    if existing is None:  # deleted between the two statements
        raise LookupError(f"manager event {dedupe_key} vanished")
    return Recorded(dict(existing), False)


async def mark_sent(event_id: int, *, pool: Db | None = None) -> None:
    """The fallback was handed to the channel (once: the first mark stays)."""
    await (pool or get_pool()).execute(
        "UPDATE manager_events SET sent_at = now(), finished_at = COALESCE(finished_at, now()) "
        "WHERE id = $1 AND sent_at IS NULL", event_id)


async def claim(manager_id: str, channel_id: str, chat: str, batch: UUID, *, limit: int = CLAIM_LIMIT,
                pool: Db | None = None) -> list[dict[str, Any]]:
    """Take the chat's pending events (the oldest ``limit``) into the completion turn ``batch``; [] when there are
    none (another caller took them, or they fell back)."""
    rows = await (pool or get_pool()).fetch(
        "UPDATE manager_events SET state = 'running', batch = $4, started_at = now() WHERE id IN ("
        "SELECT id FROM manager_events WHERE manager_id = $1 AND channel_id = $2 AND chat = $3 AND state = 'pending' "
        "ORDER BY id LIMIT $5 FOR UPDATE SKIP LOCKED) AND state = 'pending' RETURNING *",
        manager_id, channel_id, chat, batch, limit)
    return sorted((dict(row) for row in rows), key=lambda row: row["id"])


async def deliver(batch: UUID, *, record_id: int | None, pool: Db | None = None) -> list[dict[str, Any]]:
    """The completion turn's reply was handed to the channel: its running events are delivered."""
    rows = await (pool or get_pool()).fetch(
        "UPDATE manager_events SET state = 'delivered', record_id = $2, sent_at = now(), finished_at = now() "
        "WHERE batch = $1 AND state = 'running' RETURNING *", batch, record_id)
    return [dict(row) for row in rows]


async def fall_back(*, reason: str, batch: UUID | None = None, target: Sequence[str] | None = None,
                    states: Sequence[str] = ("running",), record_id: int | None = None,
                    pool: Db | None = None) -> list[dict[str, Any]]:
    """Move events to ``fallback`` with ``reason`` and answer them, oldest first, for the caller to send their code
    notices: the running events of ``batch``, or the events of the chat ``target`` (manager, channel, chat) still in
    ``states``. Moved once: a second caller gets nothing."""
    if (batch is None) == (target is None):
        raise ValueError("give a batch or a target")
    if batch is not None:
        where, args = "batch = $3", [batch]
    else:
        where, args = "manager_id = $3 AND channel_id = $4 AND chat = $5", list(target or ())
    rows = await (pool or get_pool()).fetch(
        "UPDATE manager_events SET state = 'fallback', reason = $1, finished_at = now(), "
        f"record_id = COALESCE($2, record_id) WHERE {where} AND state = ANY(${len(args) + 3}::text[]) RETURNING *",
        reason, record_id, *args, list(states))
    return sorted((dict(row) for row in rows), key=lambda row: row["id"])


async def recover(manager_id: str, *, pool: Db | None = None) -> tuple[list[dict[str, Any]], list[tuple[str, str, str]]]:
    """What a Manager that starts finds of the turns it took before: its ``running`` events (their turn was cut off)
    fall back with ``restart``. Answers the fallen-back events of the last ``UNSENT_HOURS`` whose notice was never
    handed to the channel (those just moved among them), oldest first, and the chats whose events still wait for a
    turn (``pending``)."""
    db = pool or get_pool()
    await db.execute("UPDATE manager_events SET state = 'fallback', reason = 'restart', finished_at = now() "
                     "WHERE manager_id = $1 AND state = 'running'", manager_id)
    unsent = await db.fetch(
        "SELECT * FROM manager_events WHERE manager_id = $1 AND state = 'fallback' AND sent_at IS NULL "
        "AND created_at >= now() - make_interval(hours => $2) ORDER BY id", manager_id, UNSENT_HOURS)
    waiting = await db.fetch("SELECT DISTINCT manager_id, channel_id, chat FROM manager_events "
                             "WHERE manager_id = $1 AND state = 'pending'", manager_id)
    return [dict(row) for row in unsent], [(row["manager_id"], row["channel_id"], row["chat"]) for row in waiting]


async def prune(conn: Db, before: datetime) -> int:
    """Delete one batch of events created before the cutoff."""
    result = await conn.execute(
        "DELETE FROM manager_events WHERE id IN (SELECT id FROM manager_events WHERE created_at < $1 LIMIT $2)",
        before, PRUNE_BATCH)
    return int(result.split()[-1])


async def summary(*, hours: int = SUMMARY_HOURS, pool: Db | None = None) -> dict[str, Any]:
    """Counts of the events of the last ``hours``, by state and by reason (Settings → Chat)."""
    db = pool or get_pool()
    rows = await db.fetch(
        "SELECT state, reason, count(*) AS n FROM manager_events "
        "WHERE created_at >= now() - make_interval(hours => $1) GROUP BY state, reason", hours)
    by_state: dict[str, int] = {}
    by_reason: dict[str, int] = {}
    for row in rows:
        by_state[row["state"]] = by_state.get(row["state"], 0) + row["n"]
        if row["reason"]:
            by_reason[row["reason"]] = by_reason.get(row["reason"], 0) + row["n"]
    return {"window_hours": hours, "total": sum(by_state.values()), "by_state": by_state, "by_reason": by_reason}
