"""Subscriptions: the stream configuration of a ``kind = 'subscription'`` schedule and the window each run reads
(``subscriptions``, ``subscription_runs``; message-stream RFC, section 7).

A cursor subscription reads after its ``cursor_seq``, up to its source's ``committed_seq`` as a run starts. The
cursor moves in the transaction ending the run, only from where it started: a reset meanwhile wins. A lookback
subscription ignores the cursor and reads by sent time, keeping the newest capped messages in oldest-first order.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable, Collection, Mapping, Sequence
from datetime import date, datetime
from typing import Any

import asyncpg

from .connection import get_pool

Db = asyncpg.Pool | asyncpg.Connection

# What a request sets on a subscription (``looper/agents/subscriptions.py`` validates it).
FIELDS = ("source_id", "chat_kinds", "include_chats", "exclude_chats", "sender_kinds", "exclude_mentions_me",
          "include_history", "max_age_minutes", "max_events", "render", "text_max_chars", "quiet_word",
          "output_header", "output_kind", "rules", "window", "delivery_policy")
STATE_COLUMNS = ("cursor_seq", "cursor_at", "target_confirmed_at", "target_confirmed_version")
CHAT_KINDS = ("p2p", "group", "topic", "unknown")
SENDER_KINDS = ("user", "app")


def _db(db: Db | None) -> Db:
    return db if db is not None else get_pool()


def _row(row: Mapping[str, Any]) -> dict[str, Any]:
    found = dict(row)
    if isinstance(found.get("rules"), str):
        found["rules"] = json.loads(found["rules"])
    return found


def _marks(names: Sequence[str], first: int) -> list[str]:
    return [f"${i}::jsonb" if name == "rules" else f"${i}" for i, name in enumerate(names, first)]


def _value(name: str, value: Any) -> Any:
    return json.dumps(value, ensure_ascii=False) if name == "rules" else value


async def get(schedule_id: str, *, db: Db | None = None) -> dict[str, Any] | None:
    row = await _db(db).fetchrow("SELECT * FROM subscriptions WHERE schedule_id = $1", schedule_id)
    return _row(row) if row else None


async def get_many(schedule_ids: Sequence[str], *, db: Db | None = None) -> dict[str, dict[str, Any]]:
    rows = await _db(db).fetch("SELECT * FROM subscriptions WHERE schedule_id = ANY($1::text[])", list(schedule_ids))
    return {row["schedule_id"]: _row(row) for row in rows}


async def create(conn: asyncpg.Connection, schedule_id: str, fields: Mapping[str, Any]) -> None:
    """A new subscription: cursor mode starts at the source's end; lookback leaves the unused cursor alone."""
    names = [name for name in FIELDS if name in fields]
    placeholders = ", ".join(_marks(names, 2))
    columns = ", ".join(f'"{name}"' for name in names)
    if fields.get("window", "cursor") == "lookback":
        await conn.execute(f"INSERT INTO subscriptions (schedule_id, {columns}) VALUES ($1, {placeholders})",
                           schedule_id, *(_value(name, fields[name]) for name in names))
        return
    await conn.execute(
        f"INSERT INTO subscriptions (schedule_id, {columns}, cursor_seq, cursor_at) "
        f"SELECT $1, {placeholders}, s.committed_seq, now() FROM event_sources s WHERE s.id = ${len(names) + 2}",
        schedule_id, *(_value(name, fields[name]) for name in names), fields["source_id"])


async def update(conn: asyncpg.Connection, schedule_id: str, fields: Mapping[str, Any]) -> None:
    """Set ``fields`` (``FIELDS`` and ``STATE_COLUMNS``)."""
    names = [name for name in (*FIELDS, *STATE_COLUMNS) if name in fields]
    if not names:
        return
    sets = ", ".join(f'"{name}" = {mark}' for name, mark in zip(names, _marks(names, 2)))
    await conn.execute(f"UPDATE subscriptions SET {sets} WHERE schedule_id = $1", schedule_id,
                       *(_value(name, fields[name]) for name in names))


async def committed_seq(conn: Db, source_id: str) -> int | None:
    return await conn.fetchval("SELECT committed_seq FROM event_sources WHERE id = $1", source_id)


async def cursor_after(conn: Db, schedule_id: str, moment: datetime) -> dict[str, Any]:
    """Where the cursor would go to read exactly the events ingested after ``moment``: just before the first of them
    (up to the source's ``committed_seq``), or the ``committed_seq`` when none was; with how many events lie after
    it."""
    source_id = await conn.fetchval("SELECT source_id FROM subscriptions WHERE schedule_id = $1", schedule_id)
    if source_id is None:
        raise LookupError(schedule_id)
    committed = await conn.fetchval("SELECT committed_seq FROM event_sources WHERE id = $1", source_id)
    first = await conn.fetchval(
        "SELECT min(seq) FROM source_events WHERE source_id = $1 AND ingested_at > $2 AND seq <= $3",
        source_id, moment, committed)
    cursor = (first - 1) if first is not None else committed
    pending = await conn.fetchval("SELECT count(*) FROM source_events WHERE source_id = $1 AND seq > $2 AND seq <= $3",
                                  source_id, cursor, committed)
    return {"cursor_seq": cursor, "committed_seq": committed, "events_after": pending}


class SourceBusy(RuntimeError):
    """The source is being written (a pass is reading its files)."""


LOCK_WAIT_SECONDS = 20.0


async def reset_cursor_after(conn: asyncpg.Connection, schedule_id: str, moment: datetime, *,
                             wait: float = LOCK_WAIT_SECONDS) -> dict[str, Any]:
    """Move the cursor to where ``cursor_after`` says; the new cursor, the committed sequence and the events after.
    It takes the source's writer lock first, so no write that began before ``moment`` and commits after it is left out
    of what it counts (an event's ``ingested_at`` is its transaction's start); ``SourceBusy`` when a pass holds it
    for more than ``wait`` seconds."""
    from . import message_stream

    await conn.execute("SELECT 1 FROM subscriptions WHERE schedule_id = $1 FOR UPDATE", schedule_id)
    source_id = await conn.fetchval("SELECT source_id FROM subscriptions WHERE schedule_id = $1", schedule_id)
    deadline = asyncio.get_running_loop().time() + wait
    while not await message_stream.try_lock(conn, source_id):
        if asyncio.get_running_loop().time() >= deadline:
            raise SourceBusy(source_id)
        await asyncio.sleep(0.2)
    try:
        found = await cursor_after(conn, schedule_id, moment)
        await conn.execute("UPDATE subscriptions SET cursor_seq = $2, cursor_at = now() WHERE schedule_id = $1",
                           schedule_id, found["cursor_seq"])
    finally:
        await message_stream.unlock(conn, source_id)
    return found


async def advance_cursor(conn: asyncpg.Connection, schedule_id: str, *, from_seq: int, to_seq: int,
                         at: datetime) -> bool:
    """Move the cursor from where the run started to where it read to; False when it is no longer where the run
    started (it was reset meanwhile), then it stays."""
    result = await conn.execute(
        "UPDATE subscriptions SET cursor_seq = $3, cursor_at = $4 WHERE schedule_id = $1 AND cursor_seq = $2",
        schedule_id, from_seq, to_seq, at)
    return result.endswith(" 1")


async def source_user(conn: Db, source_id: str) -> str | None:
    """The user the source is bound to (whose messages ``exclude_self`` leaves out and whose @-mentions are marked)."""
    return await conn.fetchval("SELECT user_open_id FROM event_sources WHERE id = $1", source_id)


async def claim_day(conn: Db, schedule_id: str, local_date: date) -> bool:
    """Reserve the day's attempt atomically, in the firing's transaction; never released on failure."""
    return bool(await conn.fetchval(
        "INSERT INTO subscription_daily_claims (schedule_id, local_date) VALUES ($1, $2) "
        "ON CONFLICT DO NOTHING RETURNING true", schedule_id, local_date))


def _lookback_bounds(sub: Mapping[str, Any]) -> str:
    return (" AND e.sent_at BETWEEN $4::timestamptz - make_interval(mins => $5) AND $4"
            if sub.get("window", "cursor") == "lookback" else "")


async def _window_count(conn: Db, sub: Mapping[str, Any], from_seq: int, to_seq: int, now: datetime) -> int:
    bounds = _lookback_bounds(sub)
    return await conn.fetchval(
        "SELECT count(*) FROM source_events e WHERE e.source_id = $1 AND e.seq > $2 AND e.seq <= $3" + bounds,
        sub["source_id"], from_seq, to_seq, *((now, sub["max_age_minutes"]) if bounds else ()))


async def window(conn: Db, sub: Mapping[str, Any], *, from_seq: int, to_seq: int, now: datetime,
                 own_apps: Collection[str], matcher: Callable[[Mapping[str, Any]], int | None] | None = None
                 ) -> dict[str, Any]:
    """What the subscription selects from the events after ``from_seq`` up to ``to_seq``: ``in_window`` (every event
    stored in that range), ``too_old`` (selected by the filter but sent before ``max_age_minutes``), ``over_cap``
    (older than the newest ``max_events``) and ``rows``, with chat and sender names. Lookback ignores ``from_seq``
    and bounds sent time to [now - max_age_minutes, now]; its retained rows are oldest first, cursor's newest first.
    Messages of Looper's own apps are never selected, whatever the filter says. With a ``matcher`` (the rules of the
    subscription) an event is selected when it returns an index, kept on the row as ``rule``; the subscription's own
    chat, sender and @me filter is then not applied."""
    from_seq = 0 if sub.get("window", "cursor") == "lookback" else from_seq
    if matcher is not None:
        return await _rules_window(conn, sub, from_seq=from_seq, to_seq=to_seq, now=now, own_apps=own_apps,
                                   matcher=matcher)
    found = await conn.fetch(
        f"""
        WITH base AS (
            SELECT e.seq, e.sent_at, e.chat_id, e.sender_id, e.sender_kind, e.text, c.name AS chat_name,
                   p.name AS sender_name,
                   e.sent_at >= $4::timestamptz - make_interval(mins => $5) AS fresh
            FROM source_events e
            JOIN source_chats c ON c.source_id = e.source_id AND c.chat_id = e.chat_id
            LEFT JOIN source_people p ON p.source_id = e.source_id AND p.open_id = e.sender_id
            WHERE e.source_id = $1 AND e.seq > $2 AND e.seq <= $3 {_lookback_bounds(sub)}
              AND c.inclusion = 'included' AND c.kind = ANY($6::text[])
              AND (cardinality($7::text[]) = 0 OR e.chat_id = ANY($7::text[])) AND NOT e.chat_id = ANY($8::text[])
              AND e.sender_kind = ANY($9::text[])
              AND (NOT $10::boolean OR NOT e.mentions_me)
              AND ($11::boolean OR e.origin = 'live')
              AND NOT (e.sender_kind = 'app' AND e.sender_id = ANY($12::text[]))
        )
        SELECT * FROM base ORDER BY sent_at DESC, seq DESC
        """,
        sub["source_id"], from_seq, to_seq, now, sub["max_age_minutes"], list(sub["chat_kinds"]),
        list(sub["include_chats"]), list(sub["exclude_chats"]), list(sub["sender_kinds"]),
        sub["exclude_mentions_me"], sub["include_history"], list(own_apps))
    in_window = await _window_count(conn, sub, from_seq, to_seq, now)
    fresh = [dict(row) for row in found if row["fresh"]]
    kept = fresh[:sub["max_events"]]
    if sub.get("window", "cursor") == "lookback":
        kept.reverse()
    return {"in_window": in_window, "too_old": len(found) - len(fresh), "over_cap": len(fresh) - len(kept),
            "rows": kept}


async def _rules_window(conn: Db, sub: Mapping[str, Any], *, from_seq: int, to_seq: int, now: datetime,
                        own_apps: Collection[str], matcher: Callable[[Mapping[str, Any]], int | None]) -> dict[str, Any]:
    found = await conn.fetch(
        f"""
        SELECT e.seq, e.sent_at, e.chat_id, e.sender_id, e.sender_kind, e.text, e.mentions_me, c.kind AS chat_kind,
               c.name AS chat_name, p.name AS sender_name,
               e.sent_at >= $4::timestamptz - make_interval(mins => $5) AS fresh
        FROM source_events e
        JOIN source_chats c ON c.source_id = e.source_id AND c.chat_id = e.chat_id
        LEFT JOIN source_people p ON p.source_id = e.source_id AND p.open_id = e.sender_id
        WHERE e.source_id = $1 AND e.seq > $2 AND e.seq <= $3 {_lookback_bounds(sub)} AND c.inclusion = 'included'
          AND ($6::boolean OR e.origin = 'live')
          AND NOT (e.sender_kind = 'app' AND e.sender_id = ANY($7::text[]))
        ORDER BY e.sent_at DESC, e.seq DESC
        """,
        sub["source_id"], from_seq, to_seq, now, sub["max_age_minutes"], sub["include_history"], list(own_apps))
    in_window = await _window_count(conn, sub, from_seq, to_seq, now)
    chosen = []
    for row in found:
        rule = matcher(row)
        if rule is not None:
            chosen.append({**dict(row), "rule": rule})
    fresh = [row for row in chosen if row["fresh"]]
    kept = fresh[:sub["max_events"]]
    if sub.get("window", "cursor") == "lookback":
        kept.reverse()
    return {"in_window": in_window, "too_old": len(chosen) - len(fresh), "over_cap": len(fresh) - len(kept),
            "rows": kept}


async def bump_epoch(conn: asyncpg.Connection, schedule_id: str) -> None:
    """Count a change to what a push depends on (``config_epoch`` in the schema)."""
    await conn.execute("UPDATE subscriptions SET config_epoch = config_epoch + 1 WHERE schedule_id = $1", schedule_id)


async def epoch(schedule_id: str, *, db: Db | None = None) -> int | None:
    return await _db(db).fetchval("SELECT config_epoch FROM subscriptions WHERE schedule_id = $1", schedule_id)


def snapshot(sub: Mapping[str, Any]) -> dict[str, Any]:
    """What a run selects and writes with: the subscription's request fields, as they are when it starts."""
    return {name: (list(sub[name]) if isinstance(sub[name], (list, tuple)) else sub[name]) for name in FIELDS}


async def insert_run(conn: asyncpg.Connection, run_id: str, *, from_seq: int, to_seq: int, in_window: int,
                     selected: int, too_old: int, over_cap: int, target: tuple[str, str] | None = None,
                     config_version: str | None = None, window_mode: str = "cursor", config: Mapping[str, Any] | None = None,
                     config_epoch: int | None = None) -> None:
    """The run's window and, when it started with its push target confirmed, where it may push and under which
    configuration (``config_version``)."""
    await conn.execute(
        "INSERT INTO subscription_runs (run_id, from_seq, to_seq, events_in_window, events_selected, events_too_old, "
        "events_over_cap, target_channel, target_chat, config_version, config, config_epoch, window_mode) "
        "VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11::jsonb, $12, $13)",
        run_id, from_seq, to_seq, in_window, selected, too_old, over_cap, target[0] if target else None,
        target[1] if target else None, config_version, json.dumps(config, ensure_ascii=False) if config is not None else None,
        config_epoch, window_mode)


def _run_row(row: Mapping[str, Any]) -> dict[str, Any]:
    found = dict(row)
    if isinstance(found.get("config"), str):
        found["config"] = json.loads(found["config"])
    return found


async def get_run(run_id: str, *, db: Db | None = None) -> dict[str, Any] | None:
    row = await _db(db).fetchrow("SELECT * FROM subscription_runs WHERE run_id = $1", run_id)
    return _run_row(row) if row else None


async def get_runs(run_ids: Sequence[str], *, db: Db | None = None) -> dict[str, dict[str, Any]]:
    rows = await _db(db).fetch("SELECT * FROM subscription_runs WHERE run_id = ANY($1::text[])", list(run_ids))
    return {row["run_id"]: dict(row) for row in rows}


_RUN_FIELDS = ("record_id", "outcome", "output_text", "output_sha256", "notification_id", "events_selected")


async def update_run(conn: Db, run_id: str, **fields: Any) -> None:
    unknown = sorted(set(fields) - set(_RUN_FIELDS))
    if unknown:
        raise ValueError(f"not a subscription run column: {unknown[0]}")
    sets = ", ".join(f"{name} = ${i}" for i, name in enumerate(fields, 2))
    await conn.execute(f"UPDATE subscription_runs SET {sets} WHERE run_id = $1", run_id, *fields.values())


async def undelivered_runs(*, db: Db | None = None) -> list[dict[str, Any]]:
    """Runs whose output is saved and whose push may or may not have been made: a restart finds them running."""
    rows = await _db(db).fetch(
        "SELECT r.*, sr.from_seq, sr.to_seq, sr.output_text, sr.outcome FROM schedule_runs r "
        "JOIN subscription_runs sr ON sr.run_id = r.id WHERE r.status = 'running' AND sr.outcome = 'delivering'")
    return [dict(row) for row in rows]


# --- A run's push, a message at a time ---

DELIVERY_ATTEMPTS = 3


async def create_deliveries(conn: asyncpg.Connection, run_id: str, parts: Sequence[str]) -> None:
    await conn.executemany(
        "INSERT INTO subscription_deliveries (run_id, segment, text) VALUES ($1, $2, $3) ON CONFLICT DO NOTHING",
        [(run_id, number, text) for number, text in enumerate(parts, 1)])


async def list_deliveries(run_id: str, *, db: Db | None = None) -> list[dict[str, Any]]:
    rows = await _db(db).fetch("SELECT * FROM subscription_deliveries WHERE run_id = $1 ORDER BY segment", run_id)
    return [dict(row) for row in rows]


async def deliveries_of(run_ids: Sequence[str], *, db: Db | None = None) -> dict[str, list[dict[str, Any]]]:
    rows = await _db(db).fetch(
        "SELECT run_id, segment, state, attempts, error FROM subscription_deliveries WHERE run_id = ANY($1::text[]) "
        "ORDER BY run_id, segment", list(run_ids))
    found: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        found.setdefault(row["run_id"], []).append(dict(row))
    return found


async def claim_segment(run_id: str, segment: int, *, db: Db | None = None) -> bool:
    """Write ``sending`` (committed) before the channel is asked; False when the message is not pending or failed."""
    result = await _db(db).execute(
        "UPDATE subscription_deliveries SET state = 'sending', attempts = attempts + 1, updated_at = now() "
        "WHERE run_id = $1 AND segment = $2 AND state IN ('pending', 'failed')", run_id, segment)
    return result.endswith(" 1")


async def mark_segment(run_id: str, segment: int, state: str, *, error: str | None = None,
                       external_ids: Sequence[str] = (), db: Db | None = None) -> None:
    await _db(db).execute(
        "UPDATE subscription_deliveries SET state = $3, error = $4, external_ids = $5, updated_at = now() "
        "WHERE run_id = $1 AND segment = $2", run_id, segment, state, error, list(external_ids))


async def settle_sending(run_id: str, *, db: Db | None = None) -> int:
    """Messages left ``sending`` by a process that is gone: the channel may have sent them, so they are ``uncertain``
    and are not sent again."""
    result = await _db(db).execute(
        "UPDATE subscription_deliveries SET state = 'uncertain', error = 'the server stopped while it was being sent', "
        "updated_at = now() WHERE run_id = $1 AND state = 'sending'", run_id)
    return int(result.rsplit(" ", 1)[-1])


async def uncertain_count(schedule_id: str, *, since: datetime, db: Db | None = None) -> int:
    return await _db(db).fetchval(
        "SELECT count(*) FROM subscription_deliveries d JOIN schedule_runs r ON r.id = d.run_id "
        "WHERE r.schedule_id = $1 AND d.state = 'uncertain' AND r.created_at >= $2", schedule_id, since)
