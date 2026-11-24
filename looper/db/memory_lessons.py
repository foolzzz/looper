"""Read project lessons and apply their lifecycle inside an existing transaction."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

import asyncpg

from ..agents import memory_lessons_model as lessons
from ..agents.memory_model import Action, Actor, Destination, LOOPER_STATES, Reason, SourceClass, State, Via, format_id
from .connection import get_pool
from .memory import MemoryRefused, _event, _in_transaction


async def list_lessons(*, project_id: str | None = None, state: State | str | None = None, limit: int = 50,
                       before_id: int | None = None, db: Any = None) -> tuple[list[dict[str, Any]], bool]:
    """Newest lessons first; pagination and filtering happen in SQL, including the lookahead row."""
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 200:
        raise ValueError("limit must be a whole number from 1 to 200")
    if state == "all":
        state = None
    if state is not None and state not in LOOPER_STATES:
        raise ValueError("state must be a lesson state or all")
    if before_id is not None and (isinstance(before_id, bool) or not isinstance(before_id, int) or before_id <= 0):
        raise ValueError("before must be a positive memory id")
    args: list[Any] = []
    where = ["c.destination = 'looper'"]
    for column, value, comparison in (("project_id", project_id, "="), ("state", state, "="), ("id", before_id, "<")):
        if value is not None:
            args.append(value)
            where.append(f"c.{column} {comparison} ${len(args)}")
    args.append(limit + 1)
    rows = await (db or get_pool()).fetch(
        "SELECT c.*, (SELECT count(*) FROM memory_candidate_sources WHERE candidate_id=c.id) AS sources_total, "
        "COALESCE(s.injected,0) AS stat_injected, COALESCE(s.cited,0) AS stat_cited, "
        "COALESCE(s.wrong_tasks,0) AS stat_wrong_tasks, COALESCE(s.reinforced,0) AS stat_reinforced, "
        "s.last_injected_at AS stat_last_injected_at,s.last_cited_at AS stat_last_cited_at "
        "FROM memory_candidates c LEFT JOIN memory_lesson_stats s ON s.candidate_id=c.id WHERE " +
        " AND ".join(where) + f" ORDER BY c.id DESC LIMIT ${len(args)}", *args)
    result = []
    for row in rows[:limit]:
        candidate = dict(row)
        stats = {key.removeprefix("stat_"): candidate.pop(key) for key in row.keys() if key.startswith("stat_")}
        result.append({"candidate": candidate, "stats": stats})
    return result, len(rows) > limit


async def transition_lesson(conn: asyncpg.Connection, candidate_id: int, to: State | str, *, action: Action | str,
                            actor: Actor | str, via: Via | str, now: datetime, valid_days: int,
                            reason: Reason | str | None = None, note: str | None = None,
                            expected_state: State | str | None = None) -> dict[str, Any]:
    """Record one event and move its lesson under a row lock, in the caller's transaction.

    ``expected_state`` lets a caller reject work planned before a competing transition; a rejected transition never
    adds an event. Renewals and reinforcements extend validity. Rejecting or revoking retains a fingerprint tombstone.
    """
    _in_transaction(conn)
    to, action, actor, via = State(to), Action(action), Actor(actor), Via(via)
    if isinstance(valid_days, bool) or not isinstance(valid_days, int) or valid_days < 1:
        raise MemoryRefused("valid_days must be a positive whole number", code="invalid")
    row = await conn.fetchrow("SELECT * FROM memory_candidates WHERE id=$1 FOR UPDATE", candidate_id)
    if row is None:
        raise MemoryRefused(f"no memory candidate {format_id(candidate_id)}", code="not_found")
    if row["destination"] != Destination.LOOPER:
        raise MemoryRefused(f"{format_id(candidate_id)} is a vault candidate: it has no lesson transitions",
                            code="state", state=row["state"])
    if expected_state is not None and row["state"] != expected_state:
        raise MemoryRefused("the lesson changed before this transition", code="stale", state=row["state"])
    try:
        lessons.check_lesson_transition(row["state"], to, action, actor=actor, via=via, reason=reason,
                                        source_class=SourceClass(row["source_class"]))
    except lessons.TransitionRefused as error:
        raise MemoryRefused(f"{format_id(candidate_id)}: {error}", code="state", state=row["state"]) from None
    if action in (Action.REVOKE, Action.REJECT) and via is Via.SYSTEM:
        raise MemoryRefused("a lesson is rejected or revoked from an operator interface", code="invalid")
    try:
        valid_until = (now + timedelta(days=valid_days)
                       if action in (Action.RENEW, Action.REINFORCE) else row["valid_until"])
    except OverflowError:
        raise MemoryRefused("valid_days exceeds the timestamp range", code="invalid") from None
    await _event(conn, candidate_id, action, actor, via, note=note)
    if action in (Action.REVOKE, Action.REJECT):
        await conn.execute(
            "INSERT INTO memory_tombstones(fingerprint,kind,destination,deleted_via,deleted_at) "
            "VALUES ($1,$2,$3,$4,$5) ON CONFLICT(fingerprint) DO UPDATE "
            "SET deleted_via=EXCLUDED.deleted_via,deleted_at=EXCLUDED.deleted_at",
            row["fingerprint"], row["kind"], row["destination"], via.value, now)
    return dict(await conn.fetchrow(
        "UPDATE memory_candidates SET state=$2,reason=$3,valid_until=$4,updated_at=$5 "
        "WHERE id=$1 AND state=$6 RETURNING *", candidate_id, to.value,
        Reason(reason).value if to is State.HELD and reason is not None else None, valid_until, now, row["state"]))
