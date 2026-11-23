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
    return [row["id"] for row in await (pool or get_pool()).fetch(
        "SELECT id FROM cost_holds WHERE state = 'pending' AND reason = 'unanswered' AND created_at <= $1 "
        "ORDER BY id", cutoff)]


async def claim_pause(conn, hold_id: int, now: datetime) -> dict | None:
    """Stop one pending unanswered hold, or expire it when a direct push was answered.

    The ``state = 'pending'`` claim is the only writer that pauses the schedule. Zero rows means another
    decision already owns the hold.
    """
    row = await conn.fetchrow(
        "SELECT * FROM cost_holds WHERE id = $1 AND state = 'pending' AND reason = 'unanswered' FOR UPDATE",
        hold_id)
    if row is None:
        return None
    replies = await conn.fetchval(
        "SELECT count(d.replied_at) FROM proactive_deliveries d "
        "JOIN schedule_runs r ON r.id = d.source_ref "
        "WHERE r.schedule_id = $1 AND d.source IN ('schedule', 'subscription') "
        "AND d.outcome = 'sent' AND d.chat_kind = 'direct' AND d.sent_at >= $2",
        row["scope_ref"], row["created_at"])
    if replies:
        await conn.execute(
            "UPDATE cost_holds SET state = 'expired', applied_at = $2 WHERE id = $1 AND state = 'pending'",
            hold_id, now)
        return {"paused": False, "expired": True}
    claimed = await conn.execute(
        "UPDATE cost_holds SET state = 'stopped', decided_at = $2, applied_at = $2 "
        "WHERE id = $1 AND state = 'pending'", hold_id, now)
    if not str(claimed).endswith("1"):
        return {"paused": False, "expired": False}
    name = await conn.fetchval("SELECT name FROM schedules WHERE id = $1", row["scope_ref"])
    paused = await conn.execute(
        "UPDATE schedules SET enabled = false, pause_reason = 'unanswered', paused_at = $2, next_fire_at = NULL "
        "WHERE id = $1 AND enabled", row["scope_ref"], now)
    return {"paused": str(paused).endswith("1"), "expired": False, "id": hold_id,
            "name": name, "scope_ref": row["scope_ref"]}
