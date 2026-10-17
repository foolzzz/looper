"""A task's agent room in the database: its messages, how far each seat has read, and what runs watch for.

``looper.agents.room`` owns the rules (who may post what to whom, what a seat
sees); this module only stores and reads. Messages are only ever added
(``add``); they go with their task (``ON DELETE CASCADE``) and with a restart
that plans the task anew (``db.tasks.reset_task_for_restart``).
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any

import asyncpg

from .connection import get_pool

_COLUMNS = ("id, task_id, sender_seat, sender_kind, sender_step, sender_run, via, audience_all, audience, cc_planner, "
            "kind, reason, reply_to, refs, body, planner_pending, created_at")


def _message(row: asyncpg.Record) -> dict[str, Any]:
    message = dict(row)
    message["audience"] = list(message.get("audience") or [])
    refs = message.get("refs")
    message["refs"] = json.loads(refs) if isinstance(refs, str) else list(refs or [])
    return message


async def add(task_id: str, *, sender_seat: str, sender_kind: str, kind: str, body: str,
              audience: Sequence[str] = (), audience_all: bool = False, sender_step: str | None = None,
              sender_run: str | None = None, via: str = "agent", cc_planner: bool = False, reason: str | None = None,
              reply_to: int | None = None, refs: Sequence[dict[str, Any]] = (), planner_pending: bool = False,
              pool: asyncpg.Pool | asyncpg.Connection | None = None) -> dict[str, Any]:
    """Add one message to ``task_id``'s room; the stored row."""
    pool = pool or get_pool()
    row = await pool.fetchrow(
        f"INSERT INTO room_messages (task_id, sender_seat, sender_kind, sender_step, sender_run, via, audience_all, "
        f"audience, cc_planner, kind, reason, reply_to, refs, body, planner_pending) "
        f"VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13::jsonb, $14, $15) RETURNING {_COLUMNS}",
        task_id, sender_seat, sender_kind, sender_step, sender_run, via, audience_all, list(audience), cc_planner,
        kind, reason, reply_to, json.dumps(list(refs), ensure_ascii=False), body, planner_pending,
    )
    return _message(row)


async def messages(task_id: str, *, after: int = 0, before: int | None = None, limit: int | None = None,
                   newest_first: bool = False, pool: asyncpg.Pool | None = None) -> list[dict[str, Any]]:
    """``task_id``'s messages with an id above ``after`` (and below ``before``), oldest first unless
    ``newest_first``, at most ``limit``."""
    pool = pool or get_pool()
    order = "DESC" if newest_first else "ASC"
    rows = await pool.fetch(
        f"SELECT {_COLUMNS} FROM room_messages WHERE task_id = $1 AND id > $2 AND ($3::bigint IS NULL OR id < $3) "
        f"ORDER BY id {order} LIMIT $4",
        task_id, after, before, limit,
    )
    return [_message(row) for row in rows]


async def get(task_id: str, message_id: int, *, pool: asyncpg.Pool | None = None) -> dict[str, Any] | None:
    pool = pool or get_pool()
    row = await pool.fetchrow(f"SELECT {_COLUMNS} FROM room_messages WHERE task_id = $1 AND id = $2",
                              task_id, message_id)
    return _message(row) if row is not None else None


async def count(task_id: str, *, sender_run: str | None = None, since_seconds: float | None = None,
                audience_all: bool | None = None, pool: asyncpg.Pool | None = None) -> int:
    """How many of ``task_id``'s messages there are: those ``sender_run`` posted (in the last ``since_seconds``),
    those to everyone or not (``audience_all``)."""
    pool = pool or get_pool()
    return int(await pool.fetchval(
        "SELECT count(*) FROM room_messages WHERE task_id = $1 AND ($2::text IS NULL OR sender_run = $2) "
        "AND ($3::float8 IS NULL OR created_at > now() - make_interval(secs => $3)) "
        "AND ($4::boolean IS NULL OR audience_all = $4)",
        task_id, sender_run, since_seconds, audience_all,
    ))


async def last_read(task_id: str, seat: str, *, pool: asyncpg.Pool | None = None) -> int:
    pool = pool or get_pool()
    return int(await pool.fetchval("SELECT last_read_id FROM room_reads WHERE task_id = $1 AND seat = $2",
                                   task_id, seat) or 0)


async def reads(task_id: str, *, pool: asyncpg.Pool | None = None) -> dict[str, int]:
    """How far each seat of ``task_id`` has read."""
    pool = pool or get_pool()
    rows = await pool.fetch("SELECT seat, last_read_id FROM room_reads WHERE task_id = $1", task_id)
    return {row["seat"]: int(row["last_read_id"]) for row in rows}


async def mark_read(task_id: str, seat: str, through: int, *, pool: asyncpg.Pool | None = None) -> None:
    """``seat`` has read ``task_id``'s room up to message ``through`` (never back)."""
    pool = pool or get_pool()
    await pool.execute(
        "INSERT INTO room_reads (task_id, seat, last_read_id) VALUES ($1, $2, $3) "
        "ON CONFLICT (task_id, seat) DO UPDATE SET last_read_id = GREATEST(room_reads.last_read_id, $3), "
        "updated_at = now()",
        task_id, seat, through,
    )


async def planner_pending(task_id: str, *, pool: asyncpg.Pool | None = None) -> list[dict[str, Any]]:
    """The messages that wake ``task_id``'s Planner and it has not had in a [ROOM] turn yet, oldest first."""
    pool = pool or get_pool()
    rows = await pool.fetch(f"SELECT {_COLUMNS} FROM room_messages WHERE task_id = $1 AND planner_pending "
                            "ORDER BY id", task_id)
    return [_message(row) for row in rows]


async def planner_delivered(task_id: str, ids: Sequence[int], *, pool: asyncpg.Pool | None = None) -> None:
    pool = pool or get_pool()
    await pool.execute("UPDATE room_messages SET planner_pending = false WHERE task_id = $1 AND id = ANY($2::bigint[])",
                       task_id, list(ids))


async def has_escalation(task_id: str, reason: str, *, pool: asyncpg.Pool | None = None) -> bool:
    pool = pool or get_pool()
    return bool(await pool.fetchval(
        "SELECT EXISTS (SELECT 1 FROM room_messages WHERE task_id = $1 AND kind = 'escalation' AND reason = $2)",
        task_id, reason))


# --- Watches ---


async def add_watch(task_id: str, *, seat: str, run: str, presence: str | None = None, state: str | None = None,
                    kind: str | None = None, from_seat: str | None = None, until: str = "first",
                    pool: asyncpg.Pool | None = None) -> int:
    pool = pool or get_pool()
    return int(await pool.fetchval(
        "INSERT INTO room_watches (task_id, seat, run, presence, state, kind, from_seat, until) "
        "VALUES ($1, $2, $3, $4, $5, $6, $7, $8) RETURNING id",
        task_id, seat, run, presence, state, kind, from_seat, until))


async def open_watches(task_id: str, *, run: str | None = None, pool: asyncpg.Pool | None = None,
                       ) -> list[dict[str, Any]]:
    """``task_id``'s watches that have not ended (``run``'s only, with it)."""
    pool = pool or get_pool()
    rows = await pool.fetch("SELECT * FROM room_watches WHERE task_id = $1 AND ended_at IS NULL "
                            "AND ($2::text IS NULL OR run = $2) ORDER BY id", task_id, run)
    return [dict(row) for row in rows]


async def watch_fired(watch_id: int, *, end: bool, pool: asyncpg.Pool | None = None) -> None:
    pool = pool or get_pool()
    await pool.execute("UPDATE room_watches SET fired_at = now(), ended_at = CASE WHEN $2 THEN now() END "
                       "WHERE id = $1", watch_id, end)


async def end_watches(task_id: str, run: str, *, pool: asyncpg.Pool | None = None) -> None:
    """``run`` ended: its watches end with it."""
    pool = pool or get_pool()
    await pool.execute("UPDATE room_watches SET ended_at = now() WHERE task_id = $1 AND run = $2 AND ended_at IS NULL",
                       task_id, run)


async def tasks_keeping_sessions(*, days: int, pool: asyncpg.Pool | None = None) -> list[str]:
    """The tasks whose runs' kept sessions stay: the ones that have not ended, and those that ended in the last
    ``days`` days."""
    pool = pool or get_pool()
    rows = await pool.fetch("SELECT id FROM tasks WHERE completed_at IS NULL "
                            "OR completed_at > now() - make_interval(days => $1)", days)
    return [row["id"] for row in rows]


# --- Consults ---


async def add_consult(task_id: str, *, seat: str, question: int, source_run: str | None, runtime: str | None,
                      pool: asyncpg.Pool | None = None) -> tuple[dict[str, Any], bool]:
    """``question`` for ``seat``'s consult: added to its queued one, else a new one; the row, and whether it is new."""
    pool = pool or get_pool()
    async with pool.acquire() as conn, conn.transaction():
        queued = await conn.fetchrow("SELECT * FROM room_consults WHERE task_id = $1 AND seat = $2 AND state = 'queued' "
                                     "ORDER BY id DESC LIMIT 1 FOR UPDATE", task_id, seat)
        if queued is not None:
            row = await conn.fetchrow("UPDATE room_consults SET questions = array_append(questions, $2) WHERE id = $1 "
                                      "RETURNING *", queued["id"], question)
            return dict(row), False
        row = await conn.fetchrow("INSERT INTO room_consults (task_id, seat, questions, source_run, runtime) "
                                  "VALUES ($1, $2, ARRAY[$3::bigint], $4, $5) RETURNING *",
                                  task_id, seat, question, source_run, runtime)
        return dict(row), True


async def consults(task_id: str, *, pool: asyncpg.Pool | None = None) -> list[dict[str, Any]]:
    pool = pool or get_pool()
    return [dict(r) for r in await pool.fetch("SELECT * FROM room_consults WHERE task_id = $1 ORDER BY id", task_id)]


async def consult_count(task_id: str, *, pool: asyncpg.Pool | None = None) -> int:
    pool = pool or get_pool()
    return int(await pool.fetchval("SELECT count(*) FROM room_consults WHERE task_id = $1", task_id))


async def move_consult(consult_id: int, state: str, *, expect: Sequence[str], pool: asyncpg.Pool | None = None,
                       **columns: Any) -> dict[str, Any] | None:
    """Move consult ``consult_id`` to ``state`` (and set ``columns``) when it is in one of ``expect``; the row, or None
    when it was not (another moved it first)."""
    pool = pool or get_pool()
    names = list(columns)
    sets = ", ".join(f"{name} = ${i}" for i, name in enumerate(names, 4))
    ended = ", finished_at = now()" if state in ("answered", "fallback", "failed") else ""
    row = await pool.fetchrow(
        f"UPDATE room_consults SET state = $2{', ' + sets if sets else ''}{ended} "
        f"WHERE id = $1 AND state = ANY($3::text[]) RETURNING *",
        consult_id, state, list(expect), *(columns[name] for name in names))
    return dict(row) if row is not None else None


async def latest_run(task_id: str, agent_id: str, *, pool: asyncpg.Pool | None = None) -> dict[str, Any] | None:
    """The newest record of run ``agent_id`` in ``task_id``: its session, status, metadata and output."""
    pool = pool or get_pool()
    row = await pool.fetchrow("SELECT id, agent_id, session_id, status, metadata, output_text FROM llm_records "
                              "WHERE task_id = $1 AND agent_id = $2 ORDER BY id DESC LIMIT 1", task_id, agent_id)
    if row is None:
        return None
    record = dict(row)
    if isinstance(record.get("metadata"), str):
        record["metadata"] = json.loads(record["metadata"])
    return record


async def get_consult(consult_id: int, *, pool: asyncpg.Pool | None = None) -> dict[str, Any] | None:
    pool = pool or get_pool()
    row = await pool.fetchrow("SELECT * FROM room_consults WHERE id = $1", consult_id)
    return dict(row) if row is not None else None


async def unfinished_consults(*, pool: asyncpg.Pool | None = None) -> list[dict[str, Any]]:
    """The consults of running tasks left queued or running."""
    pool = pool or get_pool()
    from .tasks import ACTIVE_STATUSES

    rows = await pool.fetch("SELECT c.* FROM room_consults c JOIN tasks t ON t.id = c.task_id "
                            "WHERE c.state IN ('queued', 'running') AND t.status = ANY($1::text[]) ORDER BY c.id",
                            list(ACTIVE_STATUSES))
    return [dict(row) for row in rows]
