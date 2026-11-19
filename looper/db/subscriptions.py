"""Subscriptions: the stream configuration of a ``kind = 'subscription'`` schedule and the window each run reads
(``subscriptions``, ``subscription_runs``; message-stream RFC, section 7).

A subscription reads the events its source ingested after its ``cursor_seq``, up to the source's ``committed_seq`` as
a run starts, so a reader never sees a gap. The cursor moves in the same transaction that ends the run, and only from
where the run started (``advance_cursor``): a reset made meanwhile wins.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping, Sequence
from datetime import datetime
from typing import Any

import asyncpg

from .connection import get_pool

Db = asyncpg.Pool | asyncpg.Connection

# What a request sets on a subscription (``looper/agents/subscriptions.py`` validates it).
FIELDS = ("source_id", "chat_kinds", "include_chats", "exclude_chats", "sender_kinds", "exclude_mentions_me",
          "include_history", "max_age_minutes", "max_events", "render", "text_max_chars", "quiet_word",
          "output_header", "output_kind")
STATE_COLUMNS = ("cursor_seq", "cursor_at", "target_confirmed_at")
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


async def reset_cursor_after(conn: asyncpg.Connection, schedule_id: str, moment: datetime) -> dict[str, Any]:
    """Move the cursor to where ``cursor_after`` says; the new cursor, the committed sequence and the events after."""
    await conn.execute("SELECT 1 FROM subscriptions WHERE schedule_id = $1 FOR UPDATE", schedule_id)
    found = await cursor_after(conn, schedule_id, moment)
    await conn.execute("UPDATE subscriptions SET cursor_seq = $2, cursor_at = now() WHERE schedule_id = $1",
                       schedule_id, found["cursor_seq"])
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


async def insert_run(conn: asyncpg.Connection, run_id: str, *, from_seq: int, to_seq: int, in_window: int,
                     selected: int, too_old: int, over_cap: int) -> None:
    await conn.execute(
        "INSERT INTO subscription_runs (run_id, from_seq, to_seq, events_in_window, events_selected, events_too_old, "
        "events_over_cap) VALUES ($1, $2, $3, $4, $5, $6, $7)", run_id, from_seq, to_seq, in_window, selected,
        too_old, over_cap)


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
