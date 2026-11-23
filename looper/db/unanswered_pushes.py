"""Reads for schedules whose direct pushes nobody answered."""

from __future__ import annotations

from datetime import datetime

from .connection import get_pool

_ASK = """
SELECT r.schedule_id, count(*) AS pushes
FROM proactive_deliveries d
JOIN schedule_runs r ON r.id = d.source_ref
JOIN schedules s ON s.id = r.schedule_id
WHERE d.source IN ('schedule', 'subscription') AND d.outcome = 'sent' AND d.chat_kind = 'direct'
  AND d.sent_at >= $1 AND d.sent_at <= $2
  AND s.enabled AND (s.unanswered_snooze_until IS NULL OR s.unanswered_snooze_until <= $4)
GROUP BY r.schedule_id
HAVING count(*) >= $3 AND count(d.replied_at) = 0
ORDER BY r.schedule_id
"""


async def schedules_to_ask(start: datetime, end: datetime, now: datetime, minimum: int, *, pool=None):
    """Direct pushes in ``[start, end]`` with no reply, for enabled schedules past their snooze."""
    return [dict(row) for row in await (pool or get_pool()).fetch(_ASK, start, end, minimum, now)]


async def due_hold_ids(cutoff: datetime, *, pool=None) -> list[int]:
    """Holds whose question was delivered at or before ``cutoff``. An unsent question has no ``asked_at``."""
    return [row["id"] for row in await (pool or get_pool()).fetch(
        "SELECT id FROM cost_holds WHERE state = 'pending' AND reason = 'unanswered' "
        "AND asked_at IS NOT NULL AND asked_at <= $1 ORDER BY id", cutoff)]


async def claim_pause(conn, hold_id: int, now: datetime, *, before_write=None) -> dict | None:
    """Stop one pending unanswered hold, or expire it when a direct push was answered after the question.

    A reply counts only when ``replied_at`` is at or after ``asked_at`` (or ``created_at`` when the question
    predates that column). ``sent_at`` does not: a reply to an older push still answers the question.

    The reply check and the state change are one ``UPDATE``. Under READ COMMITTED that command sees replies
    committed before it starts. ``FOR UPDATE`` locks the hold only, not ``proactive_deliveries``, so a reply that
    commits after the command's snapshot can still lose this pass. ``before_write`` runs after the lock and before
    that command; production leaves it unset. The ``state = 'pending'`` claim is the only writer that pauses the
    schedule. Zero rows means another decision already owns the hold.
    """
    row = await conn.fetchrow(
        "SELECT id, scope_ref FROM cost_holds "
        "WHERE id = $1 AND state = 'pending' AND reason = 'unanswered' FOR UPDATE", hold_id)
    if row is None:
        return None
    if before_write is not None:
        await before_write()
    claimed = await conn.fetchrow(
        "UPDATE cost_holds AS h SET "
        "state = CASE WHEN answered.yes THEN 'expired' ELSE 'stopped' END, "
        "decided_at = CASE WHEN answered.yes THEN h.decided_at ELSE $2 END, applied_at = $2 "
        "FROM (SELECT EXISTS (SELECT 1 FROM proactive_deliveries d "
        "JOIN schedule_runs r ON r.id = d.source_ref WHERE r.schedule_id = h2.scope_ref "
        "AND d.source IN ('schedule', 'subscription') AND d.outcome = 'sent' AND d.chat_kind = 'direct' "
        "AND d.replied_at >= COALESCE(h2.asked_at, h2.created_at)) AS yes "
        "FROM cost_holds h2 WHERE h2.id = $1) AS answered "
        "WHERE h.id = $1 AND h.state = 'pending' AND h.reason = 'unanswered' "
        "RETURNING h.state, answered.yes", hold_id, now)
    if claimed is None:
        return {"paused": False, "expired": False}
    if claimed["yes"]:
        return {"paused": False, "expired": True}
    name = await conn.fetchval("SELECT name FROM schedules WHERE id = $1", row["scope_ref"])
    paused = await conn.execute(
        "UPDATE schedules SET enabled = false, pause_reason = 'unanswered', paused_at = $2, next_fire_at = NULL "
        "WHERE id = $1 AND enabled", row["scope_ref"], now)
    return {"paused": str(paused).endswith("1"), "expired": False, "id": hold_id,
            "name": name, "scope_ref": row["scope_ref"]}
