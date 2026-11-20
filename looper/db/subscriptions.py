"""Subscriptions: the stream configuration of a ``kind = 'subscription'`` schedule and the window each run reads
(``subscriptions``, ``subscription_runs``; message-stream RFC, section 7).

A subscription reads the events its source ingested after its ``cursor_seq``, up to the source's ``committed_seq`` as
a run starts, so a reader never sees a gap. The cursor moves in the same transaction that ends the run, and only from
where the run started (``advance_cursor``): a reset made meanwhile wins.
"""

from __future__ import annotations

import asyncio
from collections.abc import Collection, Mapping, Sequence
from datetime import datetime, timedelta
from typing import Any

import asyncpg

from .connection import get_pool

Db = asyncpg.Pool | asyncpg.Connection

# What a request sets on a subscription (``looper/agents/subscriptions.py`` validates it).
FIELDS = ("source_id", "chat_kinds", "include_chats", "exclude_chats", "sender_kinds", "exclude_mentions_me",
          "include_history", "max_age_minutes", "max_events", "render", "text_max_chars", "quiet_word",
          "output_header", "output_kind")
STATE_COLUMNS = ("cursor_seq", "cursor_at", "target_confirmed_at", "target_confirmed_version")
CHAT_KINDS = ("p2p", "group", "topic", "unknown")
SENDER_KINDS = ("user", "app")


def _db(db: Db | None) -> Db:
    return db if db is not None else get_pool()


async def get(schedule_id: str, *, db: Db | None = None) -> dict[str, Any] | None:
    row = await _db(db).fetchrow("SELECT * FROM subscriptions WHERE schedule_id = $1", schedule_id)
    return dict(row) if row else None


async def get_many(schedule_ids: Sequence[str], *, db: Db | None = None) -> dict[str, dict[str, Any]]:
    rows = await _db(db).fetch("SELECT * FROM subscriptions WHERE schedule_id = ANY($1::text[])", list(schedule_ids))
    return {row["schedule_id"]: dict(row) for row in rows}


async def create(conn: asyncpg.Connection, schedule_id: str, fields: Mapping[str, Any]) -> None:
    """The subscription of a new schedule; its cursor starts at its source's ``committed_seq``, so it reads what
    arrives after it exists."""
    names = [name for name in FIELDS if name in fields]
    placeholders = ", ".join(f"${i}" for i in range(2, len(names) + 2))
    await conn.execute(
        f"INSERT INTO subscriptions (schedule_id, {', '.join(names)}, cursor_seq, cursor_at) "
        f"SELECT $1, {placeholders}, s.committed_seq, now() FROM event_sources s WHERE s.id = ${len(names) + 2}",
        schedule_id, *(fields[name] for name in names), fields["source_id"])


async def update(conn: asyncpg.Connection, schedule_id: str, fields: Mapping[str, Any]) -> None:
    """Set ``fields`` (``FIELDS`` and ``STATE_COLUMNS``)."""
    names = [name for name in (*FIELDS, *STATE_COLUMNS) if name in fields]
    if not names:
        return
    sets = ", ".join(f"{name} = ${i}" for i, name in enumerate(names, 2))
    await conn.execute(f"UPDATE subscriptions SET {sets} WHERE schedule_id = $1", schedule_id,
                       *(fields[name] for name in names))


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


async def window(conn: Db, sub: Mapping[str, Any], *, from_seq: int, to_seq: int, now: datetime,
                 own_apps: Collection[str]) -> dict[str, Any]:
    """What the subscription selects from the events after ``from_seq`` up to ``to_seq``: ``in_window`` (every event
    stored in that range), ``too_old`` (selected by the filter but sent before ``max_age_minutes``), ``over_cap``
    (older than the newest ``max_events``) and ``rows``, newest first, with the chat's and the sender's names.
    Messages of Looper's own apps are never selected, whatever the filter says."""
    found = await conn.fetch(
        """
        WITH base AS (
            SELECT e.seq, e.sent_at, e.chat_id, e.sender_id, e.sender_kind, e.text, c.name AS chat_name,
                   p.name AS sender_name,
                   e.sent_at >= $4::timestamptz - make_interval(mins => $5) AS fresh
            FROM source_events e
            JOIN source_chats c ON c.source_id = e.source_id AND c.chat_id = e.chat_id
            LEFT JOIN source_people p ON p.source_id = e.source_id AND p.open_id = e.sender_id
            WHERE e.source_id = $1 AND e.seq > $2 AND e.seq <= $3
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
    in_window = await conn.fetchval("SELECT count(*) FROM source_events WHERE source_id = $1 AND seq > $2 AND seq <= $3",
                                    sub["source_id"], from_seq, to_seq)
    fresh = [dict(row) for row in found if row["fresh"]]
    kept = fresh[:sub["max_events"]]
    return {"in_window": in_window, "too_old": len(found) - len(fresh), "over_cap": len(fresh) - len(kept),
            "rows": kept}


# What an extraction subscription reads (message-leads-intents RFC, decision 5), fixed by its output kind: the
# subscription's chat and sender kinds, its mention filter and its history switch are not consulted.
_EXTRACTION_SCOPE = {
    "leads": "e.sender_kind = 'user'",
    "intents": ("((e.sender_kind = 'user' AND (c.kind = 'p2p' OR e.mentions_me OR e.sender_id = s.user_open_id)) "
                "OR (e.sender_kind = 'app' AND e.sender_id IN "
                "(SELECT a.app_id FROM intent_alert_senders a WHERE a.source_id = e.source_id)))"),
}


def extraction_scope(output_kind: str) -> str:
    """The SQL condition of what ``output_kind`` reads, over ``source_events e``, ``source_chats c`` and
    ``event_sources s``, besides the chats, the age and Looper's own apps (``extraction_window``)."""
    if output_kind not in _EXTRACTION_SCOPE:
        raise ValueError(f"not an extraction output kind: {output_kind!r}")
    return _EXTRACTION_SCOPE[output_kind]


def _extraction_from(output_kind: str) -> str:
    """The FROM and WHERE of what an extraction reads, with ``$1`` the source, ``$2``–``$3`` the seq range, ``$4``
    and ``$5`` the subscription's included and excluded chats, ``$6`` Looper's own apps, ``$7`` whether imported
    events count."""
    return f"""
        FROM source_events e
        JOIN event_sources s ON s.id = e.source_id
        JOIN source_chats c ON c.source_id = e.source_id AND c.chat_id = e.chat_id
        WHERE e.source_id = $1 AND e.seq > $2 AND e.seq <= $3
          AND c.inclusion = 'included' AND c.extraction = 'included'
          AND (cardinality($4::text[]) = 0 OR e.chat_id = ANY($4::text[])) AND NOT e.chat_id = ANY($5::text[])
          AND ($7::boolean OR e.origin <> 'import')
          AND NOT (e.sender_kind = 'app' AND e.sender_id = ANY($6::text[]))
          AND {extraction_scope(output_kind)}"""


async def extraction_tally(conn: Db, sub: Mapping[str, Any], *, output_kind: str, since: datetime, to_seq: int,
                           own_apps: Collection[str], include_import: bool, zone_name: str,
                           from_seq: int = 0) -> list[dict[str, Any]]:
    """How many events ``output_kind`` reads among those after ``from_seq`` up to ``to_seq`` sent since ``since``, by
    chat kind and by day in ``zone_name``: rows of ``chat_kind``, ``day`` and ``events``."""
    rows = await conn.fetch(
        f"SELECT c.kind AS chat_kind, (e.sent_at AT TIME ZONE $9)::date AS day, count(*) AS events "
        f"{_extraction_from(output_kind)} AND e.sent_at >= $8 GROUP BY 1, 2 ORDER BY 2, 1",
        sub["source_id"], from_seq, to_seq, list(sub["include_chats"]), list(sub["exclude_chats"]), list(own_apps),
        include_import, since, zone_name)
    return [dict(row) for row in rows]


async def extraction_window(conn: Db, sub: Mapping[str, Any], *, output_kind: str, from_seq: int, to_seq: int,
                            now: datetime, own_apps: Collection[str], include_import: bool = False) -> dict[str, Any]:
    """Selected events with ``from_seq < seq <= to_seq``, in seq order, at most ``sub['max_events']``: the chats the
    stream and the subscription include and the extraction does not exclude, not Looper's own apps, not imported
    (unless ``include_import``: a backfill), sent within ``max_age_minutes``, and what ``output_kind`` reads
    (``extraction_scope``). Nothing is dropped: ``last_seq`` is the seq of the last row taken when more rows remain
    (``more``), else ``to_seq``; the cursor moves only to it. ``in_window`` counts every event in the range,
    ``too_old`` the selected ones sent before the age limit."""
    where = _extraction_from(output_kind)
    common = (sub["source_id"], from_seq, to_seq, list(sub["include_chats"]), list(sub["exclude_chats"]),
              list(own_apps), include_import)
    oldest = now - timedelta(minutes=sub["max_age_minutes"])
    found = await conn.fetch(
        f"""
        SELECT e.seq, e.external_id, e.chat_id, c.kind AS chat_kind, c.name AS chat_name, e.thread_id, e.root_id,
               e.parent_id, e.sender_id, e.sender_kind,
               (SELECT p.name FROM source_people p WHERE p.source_id = e.source_id AND p.open_id = e.sender_id)
                   AS sender_name,
               e.sent_at, e.mentions_me, e.mention_ids, e.text
        {where} AND e.sent_at >= $8
        ORDER BY e.seq LIMIT $9
        """, *common, oldest, sub["max_events"] + 1)
    too_old = await conn.fetchval(f"SELECT count(*) {where} AND e.sent_at < $8", *common, oldest)
    in_window = await conn.fetchval("SELECT count(*) FROM source_events WHERE source_id = $1 AND seq > $2 AND seq <= $3",
                                    sub["source_id"], from_seq, to_seq)
    rows = [dict(row) for row in found[:sub["max_events"]]]
    more = len(found) > len(rows)
    return {"in_window": in_window, "too_old": too_old, "rows": rows,
            "last_seq": rows[-1]["seq"] if more else to_seq, "more": more}


async def insert_run(conn: asyncpg.Connection, run_id: str, *, from_seq: int, to_seq: int, in_window: int,
                     selected: int, too_old: int, over_cap: int, target: tuple[str, str] | None = None,
                     config_version: str | None = None) -> None:
    """The run's window and, when it started with its push target confirmed, where it may push and under which
    configuration (``config_version``)."""
    await conn.execute(
        "INSERT INTO subscription_runs (run_id, from_seq, to_seq, events_in_window, events_selected, events_too_old, "
        "events_over_cap, target_channel, target_chat, config_version) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)",
        run_id, from_seq, to_seq, in_window, selected, too_old, over_cap, target[0] if target else None,
        target[1] if target else None, config_version)


async def get_run(run_id: str, *, db: Db | None = None) -> dict[str, Any] | None:
    row = await _db(db).fetchrow("SELECT * FROM subscription_runs WHERE run_id = $1", run_id)
    return dict(row) if row else None


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
