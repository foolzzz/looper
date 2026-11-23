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
from enum import StrEnum
from typing import Any
from uuid import UUID, uuid4

import asyncpg

from .connection import get_pool

Db = asyncpg.Pool | asyncpg.Connection

class EventKind(StrEnum):
    TASK_END = "task_end"
    TRIAGE = "triage"


class Reason(StrEnum):
    OFF = "off"
    GROUP_CHAT = "group_chat"
    MANAGER_OFF = "manager_off"
    TURN_FAILED = "turn_failed"
    TURN_TIMEOUT = "turn_timeout"
    TURN_STOPPED = "turn_stopped"
    EMPTY_REPLY = "empty_reply"
    SEND_FAILED = "send_failed"
    QUEUE_DEADLINE = "queue_deadline"
    RESTART = "restart"
    NO_CHAT = "no_chat"
    TARGET_CHANGED = "target_changed"
    MANAGER_SILENT = "manager_silent"
    DELIVERY_DEFERRED = "delivery_deferred"
    DAILY_CAP = "daily_cap"


RETENTION_DAYS = 30
# The most events one completion turn tells.
CLAIM_LIMIT = 10
# A chat's first pending event waits this long for the others ending soon after it: one turn tells them all.
WINDOW_SECONDS = 60
# A pending event that waited this long for its turn (the Manager is held by a long one) gets its notice instead.
QUEUE_SECONDS = 15 * 60
# The fallbacks of this many batches in a row, for these reasons, make the completion turns a failing feature (P3).
FAILING_BATCHES = 5
FAILING_REASONS = ("turn_failed", "turn_timeout", "empty_reply")
# A code notice the channel said it did not send (``failed``) is tried again RETRY_SECONDS * 3^(n-1) later (at most
# RETRY_MAX_SECONDS) for its first DELIVERY_ATTEMPTS tries. A notice or a reply ``sending`` for SENDING_STALE_SECONDS
# may have gone: ``uncertain``, never sent again (its send, if it answers ``sent`` later, makes it ``sent``).
RETRY_SECONDS = 5
RETRY_MAX_SECONDS = 15 * 60
DELIVERY_ATTEMPTS = 8
SENDING_STALE_SECONDS = 10 * 60
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
    # A pending event is due when its gathering window closes.
    row = await db.fetchrow(
        f"INSERT INTO manager_events ({', '.join(_COLUMNS)}, due_at) VALUES ({marks}, "
        f"CASE WHEN ${_COLUMNS.index('state') + 1} = 'pending' "
        f"THEN now() + make_interval(secs => ${len(_COLUMNS) + 1}) END) "
        "ON CONFLICT (dedupe_key) DO NOTHING RETURNING *", *(values[name] for name in _COLUMNS), float(WINDOW_SECONDS))
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


async def pending_one(manager_id: str, channel_id: str, chat: str, *, pool: Db | None = None) -> dict[str, Any] | None:
    """The chat's oldest pending event, unclaimed (what the next completion turn would tell first); None when none."""
    row = await (pool or get_pool()).fetchrow(
        "SELECT * FROM manager_events WHERE manager_id = $1 AND channel_id = $2 AND chat = $3 AND state = 'pending' "
        "ORDER BY id LIMIT 1", manager_id, channel_id, chat)
    return dict(row) if row is not None else None


async def claim(manager_id: str, channel_id: str, chat: str, batch: UUID, *, limit: int = CLAIM_LIMIT,
                kind: str = EventKind.TASK_END, pool: Db | None = None) -> list[dict[str, Any]]:
    """Take the chat's pending events of ``kind`` (the oldest ``limit``) into the turn ``batch``; [] when there are
    none (another caller took them, or they fell back). A task end and a triage event never share a batch."""
    rows = await (pool or get_pool()).fetch(
        "UPDATE manager_events SET state = 'running', batch = $4, started_at = now() WHERE id IN ("
        "SELECT id FROM manager_events WHERE manager_id = $1 AND channel_id = $2 AND chat = $3 AND state = 'pending' "
        "AND kind = $6 ORDER BY id LIMIT $5 FOR UPDATE SKIP LOCKED) AND state = 'pending' RETURNING *",
        manager_id, channel_id, chat, batch, limit, str(EventKind(kind).value))
    return sorted((dict(row) for row in rows), key=lambda row: row["id"])


async def claim_reply(batch: UUID, *, pool: Db | None = None) -> int:
    """The completion turn's reply is about to be handed to its channel: its events are ``sending`` (committed
    before the channel is asked), the batch their claim (a reply is sent once). How many."""
    result = await (pool or get_pool()).execute(
        "UPDATE manager_events SET delivery = 'sending', delivery_attempts = delivery_attempts + 1, "
        "delivery_at = now(), delivery_claim = $1 WHERE batch = $1 AND state = 'running' AND delivery IS NULL", batch)
    return int(result.split()[-1])


async def deliver(batch: UUID, *, record_id: int | None, uncertain: bool = False, error: str | None = None,
                  pool: Db | None = None) -> list[dict[str, Any]]:
    """The completion turn's reply went to the channel (``uncertain``: it may have; it is never sent again, and
    neither are its notices): its running events are delivered. A ``sent`` also corrects the events the stale sweep
    settled ``uncertain`` while this send (its batch, their claim) had not answered."""
    rows = await (pool or get_pool()).fetch(
        "UPDATE manager_events SET state = 'delivered', record_id = COALESCE($2, record_id), "
        "finished_at = COALESCE(finished_at, now()), delivery = CASE WHEN $3 THEN 'uncertain' ELSE 'sent' END, "
        "delivery_error = $4, sent_at = CASE WHEN $3 THEN NULL ELSE now() END WHERE batch = $1 AND (state = 'running' "
        "OR (NOT $3 AND state = 'delivered' AND delivery = 'uncertain' AND delivery_claim = $1)) RETURNING *",
        batch, record_id, uncertain, error)
    return [dict(row) for row in rows]


async def fall_back(*, reason: str, batch: UUID | None = None, target: Sequence[str] | None = None,
                    states: Sequence[str] = ("running",), record_id: int | None = None, rejected: bool = False,
                    pool: Db | None = None) -> list[dict[str, Any]]:
    """Move events to ``fallback`` with ``reason``, their code notices waiting for delivery (``pending``), and answer
    them, oldest first: the running events of ``batch``, or the events of the chat ``target`` (manager, channel,
    chat) still in ``states``. An event whose reply was being sent (``sending``) may have been told: it becomes
    ``delivered`` and ``uncertain`` instead, unless the channel said it did not send it (``rejected``). Moved once: a
    second caller gets nothing."""
    if (batch is None) == (target is None):
        raise ValueError("give a batch or a target")
    if batch is not None:
        where, args = "batch = $3", [batch]
    else:
        where, args = "manager_id = $3 AND channel_id = $4 AND chat = $5", list(target or ())
    states_at = len(args) + 3
    db = pool or get_pool()
    if not rejected:
        await db.execute(
            "UPDATE manager_events SET state = 'delivered', delivery = 'uncertain', finished_at = now(), "
            "delivery_error = $1, record_id = COALESCE($2, record_id) "
            f"WHERE {where} AND state = ANY(${states_at}::text[]) AND delivery = 'sending'",
            "the reply may have been sent", record_id, *args, list(states))
    rows = await db.fetch(
        "UPDATE manager_events SET state = 'fallback', reason = $1, finished_at = now(), "
        "delivery = CASE WHEN kind = 'task_end' THEN 'pending' END, "
        "delivery_attempts = 0, delivery_at = NULL, delivery_error = NULL, record_id = COALESCE($2, record_id) "
        f"WHERE {where} AND state = ANY(${states_at}::text[]) RETURNING *", reason, record_id, *args, list(states))
    return sorted((dict(row) for row in rows), key=lambda row: row["id"])


async def skip_batch(batch: UUID, *, reason: str, record_id: int | None = None,
                     pool: Db | None = None) -> list[dict[str, Any]]:
    """The running triage events of ``batch`` need nothing told (the Manager chose silence): ``skipped`` with
    ``reason``. Moved once: a second caller gets nothing."""
    rows = await (pool or get_pool()).fetch(
        "UPDATE manager_events SET state = 'skipped', reason = $2, finished_at = now(), "
        "record_id = COALESCE($3, record_id) WHERE batch = $1 AND kind = 'triage' AND state = 'running' RETURNING *",
        batch, reason, record_id)
    return sorted((dict(row) for row in rows), key=lambda row: row["id"])


async def admit_triage_batch(batch: UUID, *, start: datetime, end: datetime, cap: int, now: datetime,
                             pool: Db | None = None) -> bool:
    """Take one of the day's triage turns for ``batch``, serially: with fewer than ``cap`` other batches admitted in
    [``start``, ``end``) the batch is marked admitted (``payload.admitted_at``, persisted, so a replay is admitted
    again and a concurrent caller sees it); otherwise its running events fall back for ``daily_cap``."""
    db = pool or get_pool()
    async with db.acquire() as conn, conn.transaction():
        await conn.execute("SELECT pg_advisory_xact_lock(hashtextextended('triage-admit', 0))")
        if await conn.fetchval("SELECT bool_or(payload ? 'admitted_at') FROM manager_events WHERE batch = $1", batch):
            return True
        used = await conn.fetchval(
            "SELECT count(DISTINCT batch) FROM manager_events WHERE kind = 'triage' AND batch IS NOT NULL "
            "AND payload ? 'admitted_at' AND (payload->>'admitted_at')::timestamptz >= $1 "
            "AND (payload->>'admitted_at')::timestamptz < $2", start, end)
        if used < cap:
            await conn.execute(
                "UPDATE manager_events SET payload = payload || jsonb_build_object('admitted_at', $2::text) "
                "WHERE batch = $1 AND kind = 'triage'", batch, now.isoformat())
            return True
        await conn.execute(
            "UPDATE manager_events SET state = 'fallback', reason = 'daily_cap', finished_at = now(), delivery = NULL "
            "WHERE batch = $1 AND kind = 'triage' AND state = 'running'", batch)
        return False


async def batch_state(batch: UUID, *, pool: Db | None = None) -> tuple[str, str | None] | None:
    """The state and reason of the first event of ``batch`` (None for an unknown batch)."""
    row = await (pool or get_pool()).fetchrow(
        "SELECT state, reason FROM manager_events WHERE batch = $1 ORDER BY id LIMIT 1", batch)
    return (row["state"], row["reason"]) if row is not None else None


async def reply_open(batch: UUID, *, pool: Db | None = None) -> bool:
    """Whether ``batch`` is still running with no reply handed to a channel (it may still be told)."""
    return bool(await (pool or get_pool()).fetchval(
        "SELECT EXISTS (SELECT 1 FROM manager_events WHERE batch = $1 AND state = 'running' AND delivery IS NULL)",
        batch))


async def claim_deliveries(*, now: datetime | None = None, ids: Sequence[int] | None = None,
                           limit: int = CLAIM_LIMIT, pool: Db | None = None) -> list[dict[str, Any]]:
    """The one way a code notice is taken for sending (a fallback's, a retry's, after a restart): ``pending``, or
    ``failed`` and due again (``delivery_at`` <= ``now``, under ``DELIVERY_ATTEMPTS``), written ``sending``
    (committed) with a new ``delivery_claim`` before the channel is asked; only that claim settles it
    (``settle_delivery``). ``ids``: only these. Oldest first; a second caller gets none of them."""
    rows = await (pool or get_pool()).fetch(
        "UPDATE manager_events SET delivery = 'sending', delivery_attempts = delivery_attempts + 1, "
        "delivery_at = now(), delivery_claim = $5 WHERE id IN (SELECT id FROM manager_events WHERE state = 'fallback' "
        "AND (delivery = 'pending' OR (delivery = 'failed' AND delivery_at <= COALESCE($1::timestamptz, now()) "
        "AND delivery_attempts < $4)) AND ($3::bigint[] IS NULL OR id = ANY($3::bigint[])) "
        "ORDER BY id LIMIT $2 FOR UPDATE SKIP LOCKED) AND delivery IN ('pending', 'failed') RETURNING *",
        now, limit, list(ids) if ids is not None else None, DELIVERY_ATTEMPTS, uuid4())
    return sorted((dict(row) for row in rows), key=lambda row: row["id"])


async def settle_delivery(event_id: int, delivery: str, *, claim: UUID, error: str | None = None,
                          pool: Db | None = None) -> bool:
    """What the channel answered the send of ``claim`` for a notice: ``sent``; ``failed`` (it did not send it:
    tried again with backoff, until ``DELIVERY_ATTEMPTS``); ``uncertain`` (it may have: never again). From
    ``sending``, or a late ``sent`` from the ``uncertain`` the stale sweep settled that send as; another claim's
    answer changes nothing."""
    if delivery not in ("sent", "failed", "uncertain"):
        raise ValueError(f"not a delivery outcome: {delivery!r}")
    result = await (pool or get_pool()).execute(
        "UPDATE manager_events SET delivery = $2, delivery_error = $3, "
        "sent_at = CASE WHEN $2 = 'sent' THEN now() ELSE sent_at END, delivery_at = CASE WHEN $2 = 'failed' "
        "AND delivery_attempts < $4 THEN now() + make_interval(secs => least($5 * power(3, delivery_attempts - 1), "
        "$6)) WHEN $2 = 'failed' THEN NULL ELSE delivery_at END WHERE id = $1 AND delivery_claim = $7 "
        "AND (delivery = 'sending' OR (delivery = 'uncertain' AND $2 = 'sent'))",
        event_id, delivery, error, DELIVERY_ATTEMPTS, float(RETRY_SECONDS), float(RETRY_MAX_SECONDS), claim)
    return result.endswith(" 1")


async def settle_stale(*, before: datetime, pool: Db | None = None) -> list[dict[str, Any]]:
    """Code notices and completion replies left ``sending`` since before ``before`` (by a process that is gone, a
    send that never answered, or a reply whose record could not be written): the channel may have sent them, so they
    are ``uncertain`` and never sent again, and a reply's events are ``delivered`` (their chat's next turn may
    start)."""
    rows = await (pool or get_pool()).fetch(
        "UPDATE manager_events SET delivery = 'uncertain', delivery_error = 'its send did not answer in time: "
        "it may have gone', state = CASE WHEN state = 'running' THEN 'delivered' ELSE state END, "
        "finished_at = CASE WHEN state = 'running' THEN now() ELSE finished_at END "
        "WHERE state IN ('fallback', 'running') AND delivery = 'sending' AND delivery_at < $1 RETURNING *", before)
    return [dict(row) for row in rows]


async def recover(manager_id: str, *, pool: Db | None = None) -> int:
    """What a Manager that starts finds of the turns it took before (none of its own runs yet): an event whose reply
    was being sent may have been told (``delivered``, ``uncertain``); any other ``running`` one falls back with
    ``restart``, its notice waiting for the completion service. How many fell back."""
    db = pool or get_pool()
    await db.execute(
        "UPDATE manager_events SET state = 'delivered', delivery = 'uncertain', finished_at = now(), "
        "delivery_error = 'the server stopped while the reply was being sent' "
        "WHERE manager_id = $1 AND state = 'running' AND delivery = 'sending'", manager_id)
    result = await db.execute(
        "UPDATE manager_events SET state = 'fallback', reason = 'restart', finished_at = now(), "
        "delivery = CASE WHEN kind = 'task_end' THEN 'pending' END, "
        "delivery_attempts = 0, delivery_at = NULL WHERE manager_id = $1 AND state = 'running'", manager_id)
    return int(result.split()[-1])


async def due_targets(now: datetime, *, pool: Db | None = None) -> list[tuple[str, str, str]]:
    """The chats whose gathering window closed (their oldest pending event is due) and that no turn tells now."""
    rows = await (pool or get_pool()).fetch(
        "SELECT manager_id, channel_id, chat FROM manager_events p WHERE state = 'pending' "
        "GROUP BY manager_id, channel_id, chat HAVING min(due_at) <= $1 AND NOT EXISTS ("
        "SELECT 1 FROM manager_events r WHERE r.state = 'running' AND r.manager_id = p.manager_id "
        "AND r.channel_id = p.channel_id AND r.chat = p.chat) ORDER BY min(id)", now)
    return [(row["manager_id"], row["channel_id"], row["chat"]) for row in rows]


async def expire(now: datetime, *, pool: Db | None = None) -> list[dict[str, Any]]:
    """Pending events that waited ``QUEUE_SECONDS`` for their turn fall back (``queue_deadline``); answers them,
    oldest first, for the caller to send their notices."""
    rows = await (pool or get_pool()).fetch(
        "UPDATE manager_events SET state = 'fallback', reason = 'queue_deadline', finished_at = now(), "
        "delivery = CASE WHEN kind = 'task_end' THEN 'pending' END WHERE state = 'pending' AND created_at <= $1::timestamptz - make_interval(secs => $2) "
        "RETURNING *",
        now, float(QUEUE_SECONDS))
    return sorted((dict(row) for row in rows), key=lambda row: row["id"])


async def recover_stopped(running: Sequence[str], *, pool: Db | None = None) -> int:
    """At the service's start: the ``running`` events of Managers that do not run (none will take their turn again)
    are settled as ``recover`` settles a Manager's own. A running Manager recovered its own when it started. How
    many fell back."""
    db = pool or get_pool()
    await db.execute(
        "UPDATE manager_events SET state = 'delivered', delivery = 'uncertain', finished_at = now(), "
        "delivery_error = 'the server stopped while the reply was being sent' "
        "WHERE NOT (manager_id = ANY($1::text[])) AND state = 'running' AND delivery = 'sending'", list(running))
    result = await db.execute(
        "UPDATE manager_events SET state = 'fallback', reason = 'restart', finished_at = now(), "
        "delivery = CASE WHEN kind = 'task_end' THEN 'pending' END, "
        "delivery_attempts = 0, delivery_at = NULL WHERE NOT (manager_id = ANY($1::text[])) AND state = 'running'",
        list(running))
    return int(result.split()[-1])


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
    cost = await db.fetchval(
        "SELECT round(sum(cost_usd)::numeric, 6) FROM llm_records WHERE id IN (SELECT DISTINCT record_id "
        "FROM manager_events WHERE record_id IS NOT NULL AND created_at >= now() - make_interval(hours => $1))", hours)
    waiting = await db.fetchrow("SELECT count(*) AS n, extract(epoch FROM now() - min(created_at)) AS oldest "
                                "FROM manager_events WHERE state = 'pending'")
    return {"window_hours": hours, "total": sum(by_state.values()), "by_state": by_state, "by_reason": by_reason,
            "turn_cost_usd": float(cost) if cost is not None else 0.0, "pending": waiting["n"],
            "oldest_pending_seconds": round(float(waiting["oldest"]), 1) if waiting["oldest"] is not None else None,
            "failing": await failing(pool=db)}


async def failing(*, pool: Db | None = None) -> bool:
    """Whether the last ``FAILING_BATCHES`` completion turns all fell back for one of ``FAILING_REASONS``: the
    feature spends and does not work (the chats still got their notices)."""
    rows = await (pool or get_pool()).fetch(
        "SELECT bool_and(state = 'fallback' AND reason = ANY($2::text[])) AS failed FROM manager_events "
        "WHERE batch IS NOT NULL AND kind = 'task_end' GROUP BY batch ORDER BY max(started_at) DESC NULLS LAST LIMIT $1",
        FAILING_BATCHES, list(FAILING_REASONS))
    return len(rows) == FAILING_BATCHES and all(row["failed"] for row in rows)
