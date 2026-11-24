"""Subscriptions: the stream configuration of a ``kind = 'subscription'`` schedule and the window each run reads
(``subscriptions``, ``subscription_runs``; message-stream RFC, section 7).

A cursor subscription reads after its ``cursor_seq``, up to its source's ``committed_seq`` as a run starts. The
cursor moves in the transaction ending the run, only from where it started: a reset meanwhile wins. A lookback
subscription ignores the cursor and reads by sent time, keeping the newest capped messages in oldest-first order.
"""

from __future__ import annotations

import asyncio
import json
from collections import Counter
from dataclasses import dataclass
from collections.abc import Callable, Collection, Mapping, Sequence
from datetime import date, datetime, timedelta
from typing import Any

import asyncpg

from .connection import get_pool

Db = asyncpg.Pool | asyncpg.Connection

# What a request sets on a subscription (``looper/agents/subscriptions.py`` validates it).
FIELDS = ("source_id", "chat_kinds", "include_chats", "exclude_chats", "sender_kinds", "exclude_mentions_me",
          "include_history", "max_age_minutes", "max_events", "render", "text_max_chars", "quiet_word",
          "output_header", "output_kind", "rules", "window", "delivery_policy", "check_items")
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
    """Reserve the day's attempt in the firing's transaction, refusing dates at or before any retained claim."""
    return bool(await conn.fetchval(
        "INSERT INTO subscription_daily_claims (schedule_id, local_date) SELECT $1, $2 WHERE NOT EXISTS "
        "(SELECT 1 FROM subscription_daily_claims WHERE schedule_id = $1 AND local_date >= $2) "
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
            "rows": kept, "match_timed_out": False}


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
    prepare = getattr(matcher, "prepare", None)
    if prepare is not None:  # the text patterns run in a process that is killed at its deadline
        await prepare(found)
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
            "rows": kept, "match_timed_out": bool(getattr(matcher, "timed_out", False))}


async def bump_epoch(conn: asyncpg.Connection, schedule_id: str) -> None:
    """Count a change to what a push depends on (``config_epoch`` in the schema)."""
    await conn.execute("UPDATE subscriptions SET config_epoch = config_epoch + 1 WHERE schedule_id = $1", schedule_id)


async def epoch(schedule_id: str, *, db: Db | None = None) -> int | None:
    return await _db(db).fetchval("SELECT config_epoch FROM subscriptions WHERE schedule_id = $1", schedule_id)


def snapshot(sub: Mapping[str, Any]) -> dict[str, Any]:
    """What a run selects and writes with: the subscription's request fields, as they are when it starts."""
    return {name: (list(sub[name]) if isinstance(sub[name], (list, tuple)) else sub[name]) for name in FIELDS}


# What an extraction subscription reads (message-leads-intents RFC, decision 5), fixed by its output kind: the
# subscription's chat and sender kinds, its mention filter and its history switch are not consulted.
_EXTRACTION_SCOPE = {
    "leads": "e.sender_kind = 'user'",
    "intents": ("((e.sender_kind = 'user' AND (c.kind = 'p2p' OR e.mentions_me OR e.sender_id = s.user_open_id)) "
                "OR (e.sender_kind = 'app' AND e.sender_id IN "
                "(SELECT a.app_id FROM intent_alert_senders a WHERE a.source_id = e.source_id)))"),
}

_EXTRACTION_SCOPE["triage"] = _EXTRACTION_SCOPE["intents"]


@dataclass(frozen=True)
class GroupRead:
    """An intents run's followed-group allowance: at most ``run_max`` of their messages this run, and per chat what
    is left of today's quota (every listed chat has an entry, 0 when used up)."""
    run_max: int
    left: Mapping[str, int]


_GROUP_FROM = """
        FROM source_events e
        JOIN event_sources s ON s.id = e.source_id
        JOIN source_chats c ON c.source_id = e.source_id AND c.chat_id = e.chat_id
        JOIN intent_group_reads g ON g.source_id = e.source_id AND g.chat_id = e.chat_id
        WHERE e.source_id = $1 AND e.seq > $2 AND e.seq <= $3
          AND c.inclusion = 'included' AND c.extraction = 'included'
          AND (cardinality($4::text[]) = 0 OR e.chat_id = ANY($4::text[])) AND NOT e.chat_id = ANY($5::text[])
          AND e.sender_kind = 'user' AND e.origin = 'live'
          AND e.sent_at >= g.read_from AND e.ingested_at >= g.read_from"""

_WINDOW_COLUMNS = """e.seq, e.external_id, e.chat_id, c.kind AS chat_kind, c.name AS chat_name, e.thread_id, e.root_id,
               e.parent_id, e.sender_id, e.sender_kind,
               (SELECT p.name FROM source_people p WHERE p.source_id = e.source_id AND p.open_id = e.sender_id)
                   AS sender_name,
               e.sent_at, e.mentions_me, e.mention_ids, e.text"""


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
                           from_seq: int = 0, group_read: bool = False) -> list[dict[str, Any]]:
    """How many events ``output_kind`` reads among those after ``from_seq`` up to ``to_seq`` sent since ``since``, by
    chat kind and by day in ``zone_name``: rows of ``chat_kind``, ``day`` and ``events``."""
    rows = await conn.fetch(
        f"SELECT c.kind AS chat_kind, (e.sent_at AT TIME ZONE $9)::date AS day, count(*) AS events "
        f"{_extraction_from(output_kind)} AND e.sent_at >= $8 GROUP BY 1, 2 ORDER BY 2, 1",
        sub["source_id"], from_seq, to_seq, list(sub["include_chats"]), list(sub["exclude_chats"]), list(own_apps),
        include_import, since, zone_name)
    if group_read and output_kind == "intents":
        extra = await conn.fetch(
            f"SELECT c.kind AS chat_kind, (e.sent_at AT TIME ZONE $7)::date AS day, count(*) AS events "
            f"{_GROUP_FROM} AND NOT COALESCE({_EXTRACTION_SCOPE['intents']}, false) "
            "AND e.sent_at >= $6 GROUP BY 1, 2",
            sub["source_id"], from_seq, to_seq, list(sub["include_chats"]), list(sub["exclude_chats"]), since,
            zone_name)
        totals = Counter({(r["day"], r["chat_kind"]): r["events"] for r in rows})
        totals.update({(r["day"], r["chat_kind"]): r["events"] for r in extra})
        return [{"day": day, "chat_kind": kind, "events": n} for (day, kind), n in sorted(totals.items())]
    return [dict(row) for row in rows]


async def extraction_window(conn: Db, sub: Mapping[str, Any], *, output_kind: str, from_seq: int, to_seq: int,
                            now: datetime, own_apps: Collection[str], include_import: bool = False,
                            group_read: GroupRead | None = None) -> dict[str, Any]:
    """Selected events with ``from_seq < seq <= to_seq``, in seq order, at most ``sub['max_events']``: the chats the
    stream and the subscription include and the extraction does not exclude, not Looper's own apps, not imported
    (unless ``include_import``: a backfill), sent within ``max_age_minutes``, and what ``output_kind`` reads
    (``extraction_scope``). Nothing is dropped: ``last_seq`` is the seq of the last row taken when more rows remain
    (``more``), else ``to_seq``; the cursor moves only to it. ``in_window`` counts every event in the range,
    ``too_old`` the selected ones sent before the age limit."""
    if group_read is not None and (output_kind != "intents" or include_import):
        raise ValueError("only an intents run reads followed groups")
    where = _extraction_from(output_kind)
    if group_read is not None:
        # Every eligible followed-group human message uses the group rule and its allowance, including own/@.
        where += f" AND e.seq NOT IN (SELECT e.seq {_GROUP_FROM})"
    common = (sub["source_id"], from_seq, to_seq, list(sub["include_chats"]), list(sub["exclude_chats"]),
              list(own_apps), include_import)
    oldest = now - timedelta(minutes=sub["max_age_minutes"])
    found = await conn.fetch(
        f"""
        SELECT {_WINDOW_COLUMNS}
        {where} AND e.sent_at >= $8
        ORDER BY e.seq LIMIT $9
        """, *common, oldest, sub["max_events"] + 1)
    too_old = await conn.fetchval(f"SELECT count(*) {where} AND e.sent_at < $8", *common, oldest)
    in_window = await conn.fetchval("SELECT count(*) FROM source_events WHERE source_id = $1 AND seq > $2 AND seq <= $3",
                                    sub["source_id"], from_seq, to_seq)
    if group_read is not None:
        group_common = common[:5]
        chats = list(group_read.left)
        lefts = [group_read.left[c] for c in chats]
        extra = await conn.fetch(
            f"""SELECT x.* FROM (SELECT {_WINDOW_COLUMNS}, g.read_from, true AS via_group,
                       COALESCE({_EXTRACTION_SCOPE['intents']}, false) AS original_scope,
                       row_number() OVER (PARTITION BY e.chat_id ORDER BY e.seq) AS chat_n
                    {_GROUP_FROM} AND e.sent_at >= $6) x
                JOIN unnest($7::text[], $8::int[]) AS q(chat_id, left_n) ON q.chat_id = x.chat_id
                WHERE x.chat_n <= q.left_n ORDER BY x.seq LIMIT $9""",
            *group_common, oldest, chats, lefts, group_read.run_max + 1)
        merged = sorted([*({**dict(r), "via_group": False} for r in found),
                         *({k: v for k, v in dict(r).items() if k != "chat_n"} for r in extra)],
                        key=lambda r: r["seq"])
        rows, taken, more = [], 0, False
        for row in merged:
            if len(rows) == sub["max_events"]:
                more = True
                break
            if row["via_group"]:
                if taken == group_read.run_max:
                    more = True
                    break
                taken += 1
            rows.append(row)
        last_seq = rows[-1]["seq"] if more else to_seq
        skipped = await group_over_cap(conn, sub, from_seq=from_seq, to_seq=to_seq, now=now,
                                       last_seq=last_seq, rows=rows)
        too_old += await conn.fetchval(f"SELECT count(*) {_GROUP_FROM} AND e.sent_at < $6", *group_common, oldest)
        return {"in_window": in_window, "too_old": too_old, "rows": rows, "last_seq": last_seq, "more": more,
                "group_over_cap": skipped}
    rows = [dict(row) for row in found[:sub["max_events"]]]
    more = len(found) > len(rows)
    return {"in_window": in_window, "too_old": too_old, "rows": rows,
            "last_seq": rows[-1]["seq"] if more else to_seq, "more": more}


async def group_over_cap(conn: Db, sub: Mapping[str, Any], *, from_seq: int, to_seq: int, now: datetime,
                         last_seq: int, rows: Sequence[Mapping[str, Any]]) -> dict[str, int]:
    """Only quota-skipped group messages the final cursor actually moves past, after any input trimming."""
    fresh = {r["chat_id"]: r["n"] for r in await conn.fetch(
        f"SELECT e.chat_id, count(*) AS n {_GROUP_FROM} AND e.sent_at >= $6 AND e.seq <= $7 GROUP BY 1",
        sub["source_id"], from_seq, to_seq, list(sub["include_chats"]), list(sub["exclude_chats"]),
        now - timedelta(minutes=sub["max_age_minutes"]), last_seq)}
    took = Counter(r["chat_id"] for r in rows if r.get("via_group"))
    return {c: fresh[c] - took[c] for c in fresh if fresh[c] > took[c]}


async def group_read_left(conn: Db, source_id: str, day: date, day_max: int) -> dict[str, int]:
    """Each followed chat of the source and what is left of its quota on ``day``."""
    rows = await conn.fetch(
        "SELECT g.chat_id, COALESCE(d.read, 0) AS read FROM intent_group_reads g LEFT JOIN intent_group_read_days d "
        "ON d.source_id = g.source_id AND d.chat_id = g.chat_id AND d.day = $2 WHERE g.source_id = $1", source_id, day)
    return {r["chat_id"]: max(0, day_max - r["read"]) for r in rows}


async def count_group_day(conn: Db, source_id: str, day: date, *, read: Mapping[str, int],
                          skipped: Mapping[str, int]) -> None:
    """Add to the day's counts of each chat (one upsert per chat) and forget days older than 30."""
    for chat_id in sorted(read.keys() | skipped.keys()):
        await conn.execute(
            "INSERT INTO intent_group_read_days (source_id, chat_id, day, read, skipped) VALUES ($1, $2, $3, $4, $5) "
            "ON CONFLICT (source_id, chat_id, day) DO UPDATE SET read = intent_group_read_days.read + EXCLUDED.read, "
            "skipped = intent_group_read_days.skipped + EXCLUDED.skipped",
            source_id, chat_id, day, read.get(chat_id, 0), skipped.get(chat_id, 0))
    await conn.execute("DELETE FROM intent_group_read_days WHERE day < $1::date - 30", day)


async def insert_run(conn: asyncpg.Connection, run_id: str, *, from_seq: int, to_seq: int, in_window: int,
                     selected: int, too_old: int, over_cap: int, target: tuple[str, str] | None = None,
                     config_version: str | None = None, window_mode: str = "cursor", config: Mapping[str, Any] | None = None,
                     config_epoch: int | None = None, daily_date: date | None = None, match_timeouts: int = 0) -> None:
    """The run's window and, when it started with its push target confirmed, where it may push and under which
    configuration (``config_version``)."""
    await conn.execute(
        "INSERT INTO subscription_runs (run_id, from_seq, to_seq, events_in_window, events_selected, events_too_old, "
        "events_over_cap, target_channel, target_chat, config_version, config, config_epoch, window_mode, daily_date, "
        "match_timeouts) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11::jsonb, $12, $13, $14, $15)",
        run_id, from_seq, to_seq, in_window, selected, too_old, over_cap, target[0] if target else None,
        target[1] if target else None, config_version, json.dumps(config, ensure_ascii=False) if config is not None else None,
        config_epoch, window_mode, daily_date, match_timeouts)


def _run_row(row: Mapping[str, Any]) -> dict[str, Any]:
    found = dict(row)
    if isinstance(found.get("config"), str):
        found["config"] = json.loads(found["config"])
    # Keep non-triage readbacks unchanged; only triage runs opt into exact window bookkeeping.
    if found.get("window_seqs") is None:
        found.pop("window_seqs", None)
    return found


async def runs_for_message(seq: int, *, db: Db | None = None) -> list[dict[str, Any]]:
    """Runs whose selected window contains this exact message, including uncited and skipped windows.

    A seq between from_seq and to_seq is not necessarily selected. No source text is read or retained here.
    """
    # Array containment is the operator the GIN index on window_seqs serves.
    rows = await _db(db).fetch(
        "SELECT * FROM subscription_runs WHERE window_seqs @> ARRAY[$1]::bigint[] ORDER BY run_id", seq)
    return [_run_row(row) for row in rows]


async def get_run(run_id: str, *, db: Db | None = None) -> dict[str, Any] | None:
    row = await _db(db).fetchrow("SELECT * FROM subscription_runs WHERE run_id = $1", run_id)
    return _run_row(row) if row else None


async def get_runs(run_ids: Sequence[str], *, db: Db | None = None) -> dict[str, dict[str, Any]]:
    rows = await _db(db).fetch("SELECT * FROM subscription_runs WHERE run_id = ANY($1::text[])", list(run_ids))
    return {row["run_id"]: dict(row) for row in rows}


_RUN_FIELDS = ("record_id", "outcome", "output_text", "output_sha256", "notification_id", "events_selected",
               "skip_reason")


async def check_failures(conn: Db, schedule_id: str, *, limit: int) -> int:
    """How many of the subscription's latest runs whose check ran (``check_status`` checked or failed; skipped ones
    neither end nor add to it) failed in a row, counting at most ``limit``."""
    rows = await conn.fetch(
        "SELECT sr.check_status FROM subscription_runs sr JOIN schedule_runs r ON r.id = sr.run_id "
        "WHERE r.schedule_id = $1 AND sr.check_status IN ('checked', 'failed') "
        "ORDER BY r.created_at DESC, r.id DESC LIMIT $2", schedule_id, limit)
    return next((index for index, row in enumerate(rows) if row["check_status"] != "failed"), len(rows))


async def set_check_alert(conn: Db, schedule_id: str, at: datetime | None) -> bool:
    """Start (``at``) or end (None) the subscription's failed-check alert, once: False when it already was so."""
    condition = "check_alert_at IS NULL" if at is not None else "check_alert_at IS NOT NULL"
    return await conn.execute(f"UPDATE subscriptions SET check_alert_at = $2 WHERE schedule_id = $1 AND {condition}",
                              schedule_id, at) == "UPDATE 1"


async def skip_streak(conn: Db, schedule_id: str, *, limit: int) -> int:
    """How many of the subscription's latest completed runs skipped their window in a row, counting at most
    ``limit``; a run that did not complete neither ends nor adds to it."""
    rows = await conn.fetch(
        "SELECT sr.outcome FROM subscription_runs sr JOIN schedule_runs r ON r.id = sr.run_id "
        "WHERE r.schedule_id = $1 AND r.status = 'completed' ORDER BY r.created_at DESC, r.id DESC LIMIT $2",
        schedule_id, limit)
    return next((index for index, row in enumerate(rows) if row["outcome"] != "skipped"), len(rows))


async def set_skip_alert(conn: Db, schedule_id: str, at: datetime | None) -> bool:
    """Start (``at``) or end (None) the subscription's skipped-window alert, once: False when it already was so."""
    condition = "skip_alert_at IS NULL" if at is not None else "skip_alert_at IS NOT NULL"
    return await conn.execute(f"UPDATE subscriptions SET skip_alert_at = $2 WHERE schedule_id = $1 AND {condition}",
                              schedule_id, at) == "UPDATE 1"


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
