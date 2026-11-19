"""The message stream's tables: sources, their chats and people, events and runs (message-stream RFC, section 4).

One writer per source: ``try_lock`` takes a PostgreSQL advisory lock on a connection the caller keeps for a pass.
Every file or chat commits its events, its state and ``committed_seq`` in one transaction (``ingest``), so a pass that
dies loses only the transaction in flight and a reader of ``committed_seq`` never sees a gap.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any

import asyncpg

from .connection import get_pool

INGEST_MODES = ("off", "bridge", "collect")
CHAT_KINDS = ("p2p", "group", "topic", "unknown")
RUN_MODES = ("bridge", "collect", "listing", "import")
STALE_AFTER_SECONDS = 3600
_BATCH = 500
_LOCK_CLASS = 0x6D736667  # "msfg"

Db = asyncpg.Pool | asyncpg.Connection

_SOURCE_COLUMNS = ("id", "kind", "name", "lark_profile", "user_open_id", "ingest_mode", "bridge_dir", "bridge_start_from", "cron",
                   "timezone", "committed_seq", "last_run_at", "last_ok_at", "body_retention_days",
                   "app_body_retention_days", "created_at", "updated_at")


def _db(db: Db | None) -> Db:
    return db if db is not None else get_pool()


async def get_source(source_id: str, *, db: Db | None = None) -> dict[str, Any] | None:
    row = await _db(db).fetchrow(f"SELECT {', '.join(_SOURCE_COLUMNS)} FROM event_sources WHERE id = $1", source_id)
    return dict(row) if row else None


async def list_sources(*, db: Db | None = None) -> list[dict[str, Any]]:
    rows = await _db(db).fetch(f"SELECT {', '.join(_SOURCE_COLUMNS)} FROM event_sources ORDER BY created_at, id")
    return [dict(row) for row in rows]


async def save_source(source_id: str, fields: Mapping[str, Any], *, db: Db | None = None) -> dict[str, Any]:
    """Create the source with its defaults, or change the fields given. The caller validated them."""
    names = list(fields)
    async with _transaction(db) as conn:
        await conn.execute("INSERT INTO event_sources (id, kind, name) VALUES ($1, 'lark_user', $2) "
                           "ON CONFLICT (id) DO NOTHING", source_id, fields.get("name") or source_id)
        if names:
            sets = ", ".join(f"{name} = ${index}" for index, name in enumerate(names, 2))
            await conn.execute(f"UPDATE event_sources SET {sets}, updated_at = now() WHERE id = $1",
                               source_id, *(fields[name] for name in names))
        row = await conn.fetchrow(f"SELECT {', '.join(_SOURCE_COLUMNS)} FROM event_sources WHERE id = $1", source_id)
    return dict(row)


class _transaction:
    """A transaction on a pool, or the caller's connection (its own transaction when it has none)."""

    def __init__(self, db: Db | None) -> None:
        self._db = _db(db)
        self._conn: asyncpg.Connection | None = None
        self._tx: Any = None

    async def __aenter__(self) -> asyncpg.Connection:
        if isinstance(self._db, asyncpg.Pool):
            self._conn = await self._db.acquire()
        else:
            self._conn = self._db
        self._tx = self._conn.transaction()
        await self._tx.start()
        return self._conn

    async def __aexit__(self, *exc: Any) -> None:
        try:
            if exc[0] is None:
                await self._tx.commit()
            else:
                await self._tx.rollback()
        finally:
            if isinstance(self._db, asyncpg.Pool):
                await self._db.release(self._conn)


def transaction(db: Db | None = None) -> _transaction:
    return _transaction(db)


async def try_lock(conn: asyncpg.Connection, source_id: str) -> bool:
    """Take the source's writer lock for this connection's session; False when another session holds it."""
    return bool(await conn.fetchval(
        "SELECT pg_try_advisory_lock($1, hashtext(current_schema() || ':' || $2))", _LOCK_CLASS, source_id))


async def unlock(conn: asyncpg.Connection, source_id: str) -> None:
    await conn.execute("SELECT pg_advisory_unlock($1, hashtext(current_schema() || ':' || $2))", _LOCK_CLASS, source_id)


async def interrupt_stale_runs(conn: asyncpg.Connection, source_id: str) -> int:
    """Runs left ``running`` by a pass that died: the holder of the lock knows no other pass is writing."""
    result = await conn.execute(
        "UPDATE source_runs SET status = 'interrupted', finished_at = now() "
        "WHERE source_id = $1 AND status = 'running'", source_id)
    return int(result.rsplit(" ", 1)[-1])


async def start_run(conn: asyncpg.Connection, source_id: str, mode: str, trigger: str, *, now: datetime) -> int:
    run_id = await conn.fetchval(
        "INSERT INTO source_runs (source_id, mode, trigger, status, started_at) VALUES ($1, $2, $3, 'running', $4) "
        "RETURNING id", source_id, mode, trigger, now)
    await conn.execute("UPDATE event_sources SET last_run_at = $2 WHERE id = $1", source_id, now)
    return run_id


async def finish_run(conn: asyncpg.Connection, run_id: int, status: str, *, now: datetime, error: str | None = None,
                     files_rescanned: int = 0, ok_for: str | None = None) -> None:
    await conn.execute("UPDATE source_runs SET status = $2, finished_at = $3, error = $4, files_rescanned = $5 "
                       "WHERE id = $1", run_id, status, now, error, files_rescanned)
    if status == "ok" and ok_for:
        await conn.execute("UPDATE event_sources SET last_ok_at = $2 WHERE id = $1", ok_for, now)


async def prune_runs(conn: asyncpg.Connection, source_id: str, *, before: datetime) -> int:
    result = await conn.execute("DELETE FROM source_runs WHERE source_id = $1 AND finished_at < $2", source_id, before)
    return int(result.rsplit(" ", 1)[-1])


async def chat_for_update(conn: asyncpg.Connection, source_id: str, chat_id: str, *, kind: str,
                          name: str) -> dict[str, Any]:
    """The chat's row, created when it is new, locked for this transaction."""
    await conn.execute("INSERT INTO source_chats (source_id, chat_id, kind, name) VALUES ($1, $2, $3, $4) "
                       "ON CONFLICT DO NOTHING", source_id, chat_id, kind, name)
    return dict(await conn.fetchrow("SELECT * FROM source_chats WHERE source_id = $1 AND chat_id = $2 FOR UPDATE",
                                    source_id, chat_id))


async def set_chat_directory(conn: asyncpg.Connection, source_id: str, chat_id: str, *, kind: str | None,
                             name: str | None) -> None:
    await conn.execute("UPDATE source_chats SET kind = COALESCE($3, kind), name = COALESCE($4, name) "
                       "WHERE source_id = $1 AND chat_id = $2 AND (kind IS DISTINCT FROM COALESCE($3, kind) "
                       "OR name IS DISTINCT FROM COALESCE($4, name))", source_id, chat_id, kind, name)


async def exclude_chat(conn: asyncpg.Connection, source_id: str, chat_id: str, reason: str, *, now: datetime) -> None:
    await conn.execute("UPDATE source_chats SET inclusion = 'excluded', exclusion_reason = $3, excluded_at = $4 "
                       "WHERE source_id = $1 AND chat_id = $2 AND inclusion = 'included'",
                       source_id, chat_id, reason, now)


async def save_bridge_state(conn: asyncpg.Connection, source_id: str, chat_id: str, *, offset: int, size: int,
                            inode: str, last_id: str | None, mtime: datetime, newest: datetime | None,
                            now: datetime) -> None:
    await conn.execute(
        "UPDATE source_chats SET bridge_offset = $3, bridge_size = $4, bridge_inode = $5, bridge_last_id = $6, "
        "bridge_mtime = $7, last_pulled_at = $8, last_message_at = GREATEST(last_message_at, $9) "
        "WHERE source_id = $1 AND chat_id = $2", source_id, chat_id, offset, size, inode, last_id, mtime, now, newest)


async def insert_events(conn: asyncpg.Connection, source_id: str, run_id: int | None,
                        rows: Sequence[Mapping[str, Any]]) -> list[tuple[str, int]]:
    """Insert in the order given and keep the first copy of a message already stored: (external_id, seq) of the rows
    that were new. A row whose ``body`` is None is stored without its raw JSON and marked dropped (retention)."""
    inserted: list[tuple[str, int]] = []
    for start in range(0, len(rows), _BATCH):
        batch = rows[start:start + _BATCH]

        def column(name: str) -> list[Any]:
            return [row[name] for row in batch]
        found = await conn.fetch(
            """INSERT INTO source_events (source_id, external_id, chat_id, thread_id, root_id, parent_id, sender_id,
                   sender_kind, sent_at, msg_type, mentions_me, mention_ids, text, body, body_bytes, body_dropped_at, origin, run_id)
               SELECT $1, t.external_id, t.chat_id, t.thread_id, t.root_id, t.parent_id, t.sender_id, t.sender_kind,
                      t.sent_at, t.msg_type, t.mentions_me, COALESCE(string_to_array(NULLIF(t.mention_ids, ''), ','), '{}'), t.text,
                      t.body::jsonb, t.body_bytes, CASE WHEN t.body IS NULL THEN now() END, t.origin, $2
               FROM unnest($3::text[], $4::text[], $5::text[], $6::text[], $7::text[], $8::text[], $9::text[],
                           $10::timestamptz[], $11::text[], $12::boolean[], $13::text[], $14::text[], $15::text[],
                           $16::int[], $17::text[])
                    WITH ORDINALITY AS t(external_id, chat_id, thread_id, root_id, parent_id, sender_id, sender_kind,
                                         sent_at, msg_type, mentions_me, mention_ids, text, body, body_bytes, origin, n)
               ORDER BY t.n
               ON CONFLICT (source_id, external_id) DO NOTHING
               RETURNING external_id, seq""",
            source_id, run_id, column("external_id"), column("chat_id"), column("thread_id"), column("root_id"),
            column("parent_id"), column("sender_id"), column("sender_kind"), column("sent_at"), column("msg_type"),
            column("mentions_me"), [",".join(r["mention_ids"]) for r in batch], column("text"),
            [None if r["body"] is None else json.dumps(r["body"], ensure_ascii=False) for r in batch], column("body_bytes"), column("origin"))
        inserted.extend((row["external_id"], row["seq"]) for row in found)
    return inserted


async def advance_committed(conn: asyncpg.Connection, source_id: str, seq: int) -> None:
    await conn.execute("UPDATE event_sources SET committed_seq = GREATEST(committed_seq, $2) WHERE id = $1",
                       source_id, seq)


async def add_run_counts(conn: asyncpg.Connection, run_id: int, *, new: int, seen: int, skipped: int) -> None:
    await conn.execute("UPDATE source_runs SET events_new = events_new + $2, events_seen = events_seen + $3, "
                       "events_skipped = events_skipped + $4 WHERE id = $1", run_id, new, seen, skipped)


async def upsert_people(conn: asyncpg.Connection, source_id: str,
                        people: Mapping[str, tuple[str, str]], *, now: datetime) -> None:
    """open_id -> (name, name_source). A contact name is never replaced by a weaker one."""
    if not people:
        return
    ids = list(people)
    await conn.execute(
        """INSERT INTO source_people (source_id, open_id, name, name_source, refreshed_at)
           SELECT $1, t.open_id, t.name, t.name_source, $5
           FROM unnest($2::text[], $3::text[], $4::text[]) AS t(open_id, name, name_source)
           ON CONFLICT (source_id, open_id) DO UPDATE SET name = EXCLUDED.name, name_source = EXCLUDED.name_source,
               refreshed_at = EXCLUDED.refreshed_at
           WHERE source_people.name IS DISTINCT FROM EXCLUDED.name OR source_people.name_source <> EXCLUDED.name_source""",
        source_id, ids, [people[i][0] for i in ids], [people[i][1] for i in ids], now)


async def known_people(conn: asyncpg.Connection, source_id: str) -> list[str]:
    return [r["open_id"] for r in await conn.fetch("SELECT open_id FROM source_people WHERE source_id = $1", source_id)]


async def status_row(source_id: str, *, db: Db | None = None) -> dict[str, Any]:
    """Counts only: never a message's text."""
    db = _db(db)
    row = await db.fetchrow(
        """SELECT (SELECT count(*) FROM source_events e WHERE e.source_id = s.id) AS events,
                  (SELECT count(*) FROM source_events e WHERE e.source_id = s.id
                      AND e.ingested_at > now() - interval '1 hour') AS events_last_hour,
                  (SELECT max(e.sent_at) FROM source_events e WHERE e.source_id = s.id) AS newest_sent_at,
                  (SELECT coalesce(sum(e.body_bytes) FILTER (WHERE e.body IS NOT NULL), 0)
                      FROM source_events e WHERE e.source_id = s.id) AS body_bytes,
                  (SELECT count(*) FROM source_chats c WHERE c.source_id = s.id) AS chats,
                  (SELECT count(*) FROM source_chats c WHERE c.source_id = s.id
                      AND c.inclusion = 'excluded') AS chats_excluded,
                  (SELECT count(*) FROM source_chats c WHERE c.source_id = s.id AND c.state = 'failing') AS chats_failing,
                  now() AS now
           FROM event_sources s WHERE s.id = $1""", source_id)
    return dict(row)


async def list_runs(source_id: str, *, limit: int, db: Db | None = None) -> list[dict[str, Any]]:
    rows = await _db(db).fetch(
        "SELECT id, mode, trigger, status, started_at, finished_at, events_new, events_seen, events_skipped, "
        "files_rescanned, error FROM source_runs WHERE source_id = $1 ORDER BY id DESC LIMIT $2", source_id, limit)
    return [dict(row) for row in rows]
