"""Completion events: one row per task end that has a chat to tell (manager completion ledger RFC).

The row keeps the code notice the end would send (``fallback``) so that whatever happens to a later Manager turn,
the notice goes out exactly as it always did. ``dedupe_key`` makes a replayed message record once.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import asyncpg

from .connection import get_pool

Db = asyncpg.Pool | asyncpg.Connection

RETENTION_DAYS = 30
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
    row = await db.fetchrow(
        f"INSERT INTO manager_events ({', '.join(_COLUMNS)}) VALUES ({marks}) "
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
