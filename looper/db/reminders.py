"""The ``reminders`` table (reminders RFC): the user's one-time reminders and what became of each.

A reminder is ``pending`` until it is due. Only the delivery (``agents.reminders.ReminderService``) moves it on: it
claims one as ``sending`` (committed before the channel is asked) and settles it ``delivered``, ``failed`` or
``uncertain``, or settles one Looper could not send on time as ``missed``. Only the user cancels one
(``cancelled``), while it is ``pending`` or to dismiss a terminal not-delivered state.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from .connection import get_pool

STATUSES = ("pending", "sending", "delivered", "failed", "uncertain", "missed", "cancelled")
LIST_DEFAULT, LIST_MAX = 20, 50
ATTENTION_WINDOW = timedelta(days=7)


def _db(db: Any) -> Any:
    return db if db is not None else get_pool()


async def create(*, text: str, due_at: datetime, timezone: str, channel_id: str, chat: str, created_channel: str,
                 created_message_id: str | None, db: Any = None) -> dict[str, Any]:
    """Store a new pending reminder and return it as the table now holds it (read back, not echoed)."""
    pool = _db(db)
    reminder_id = await pool.fetchval(
        "INSERT INTO reminders (text, due_at, timezone, channel_id, chat, created_channel, created_message_id) "
        "VALUES ($1, $2, $3, $4, $5, $6, $7) RETURNING id",
        text, due_at, timezone, channel_id, chat, created_channel, created_message_id)
    found = await get(reminder_id, db=pool)
    assert found is not None
    return found


async def get(reminder_id: int, *, db: Any = None) -> dict[str, Any] | None:
    row = await _db(db).fetchrow("SELECT * FROM reminders WHERE id = $1", reminder_id)
    return dict(row) if row is not None else None


async def list_reminders(*, status: str | None, limit: int = LIST_DEFAULT,
                         db: Any = None) -> tuple[list[dict[str, Any]], bool]:
    """Reminders of ``status`` (None: all), at most ``limit``, and whether there are more: pending ones soonest
    first, the others latest first."""
    order = "due_at, id" if status == "pending" else "due_at DESC, id DESC"
    rows = await _db(db).fetch(
        f"SELECT * FROM reminders WHERE $1::text IS NULL OR status = $1 ORDER BY {order} LIMIT $2",
        status, limit + 1)
    return [dict(row) for row in rows[:limit]], len(rows) > limit


async def next_pending(*, db: Any = None) -> dict[str, Any] | None:
    """The soonest pending reminder, or None when none is pending."""
    row = await _db(db).fetchrow(
        "SELECT * FROM reminders WHERE status = 'pending' ORDER BY due_at, id LIMIT 1")
    return dict(row) if row is not None else None


async def needing_attention(*, now: datetime, db: Any = None, channel_id: str | None = None,
                            chat: str | None = None) -> list[dict[str, Any]]:
    """Uncancelled terminal failures, for seven days after settlement; one row per reminder, oldest first.

    The brief can narrow this read to its target. Reading never retries or records a delivery.
    """
    rows = await _db(db).fetch(
        "SELECT * FROM reminders WHERE status IN ('uncertain', 'failed', 'missed') "
        "AND settled_at > $1 AND settled_at <= $2 "
        "AND ($3::text IS NULL OR channel_id = $3) AND ($4::text IS NULL OR chat = $4) "
        "ORDER BY settled_at, id", now - ATTENTION_WINDOW, now, channel_id, chat)
    return [dict(row) for row in rows]


async def cancel(reminder_id: int, *, now: datetime, db: Any = None) -> tuple[dict[str, Any] | None, bool]:
    """Cancel a pending reminder or dismiss a terminal failure: (readback, changed); (None, False) if absent."""
    pool = _db(db)
    row = await pool.fetchrow(
        "UPDATE reminders SET status = 'cancelled', detail = CASE WHEN status = 'pending' "
        "THEN 'cancelled by the user' ELSE 'cancelled by the user after ' || status END, settled_at = $2, "
        "updated_at = $2 WHERE id = $1 AND status IN ('pending', 'uncertain', 'failed', 'missed') RETURNING *",
        reminder_id, now)
    if row is not None:
        return dict(row), True
    return await get(reminder_id, db=pool), False


# The delivery's steps; each runs on the connection that holds the delivery's lock.

async def recover(conn: Any, now: datetime) -> tuple[int, int]:
    """Recover under the sender lock: unstarted claims retry; started sends never repeat.

    Reminder states and existing decision receipts commit together, including a receipt lost after settle.
    No decision is invented when the delivery layer was off or its decision write failed.
    """
    async with conn.transaction():
        retried = await conn.execute(
            "UPDATE reminders SET status = 'pending', updated_at = $1 "
            "WHERE status = 'sending' AND post_started_at IS NULL", now)
        uncertain = await conn.execute(
            "UPDATE reminders SET status = 'uncertain', detail = 'the sender stopped before recording the answer', "
            "settled_at = $1, updated_at = $1 WHERE status = 'sending' AND post_started_at IS NOT NULL", now)
        await conn.execute(
            "UPDATE proactive_deliveries d SET outcome = CASE r.status "
            "WHEN 'delivered' THEN 'sent' WHEN 'failed' THEN 'rejected' ELSE 'unknown' END, "
            "sent_at = r.settled_at, external_id = r.external_id FROM reminders r "
            "WHERE d.dedupe_key = 'looper-reminder-' || r.id::text AND d.decision = 'send' "
            "AND (d.outcome IS NULL OR (d.outcome = 'rejected' AND r.status <> 'failed')) "
            "AND r.post_started_at IS NOT NULL "
            "AND r.status IN ('delivered', 'failed', 'uncertain')")
    return int(retried.split()[-1]), int(uncertain.split()[-1])


async def due(conn: Any, now: datetime) -> list[dict[str, Any]]:
    rows = await conn.fetch("SELECT * FROM reminders WHERE status = 'pending' AND due_at <= $1 ORDER BY due_at, id",
                            now)
    return [dict(row) for row in rows]


async def settle(conn: Any, reminder_id: int, status: str, *, now: datetime, detail: str | None = None,
                 external_id: str | None = None, from_status: str = "sending") -> bool:
    """Move one reminder from ``from_status`` to a settled ``status``; False when it was not in ``from_status``."""
    return await conn.fetchval(
        "UPDATE reminders SET status = $2, detail = $3, external_id = $4, settled_at = $5, updated_at = $5 "
        "WHERE id = $1 AND status = $6 RETURNING true",
        reminder_id, status, detail, external_id, now, from_status) is True


async def claim(conn: Any, reminder_id: int, now: datetime) -> bool:
    """Take a pending reminder for one send (committed before the channel is asked)."""
    return await conn.fetchval(
        "UPDATE reminders SET status = 'sending', attempts = attempts + 1, post_started_at = NULL, updated_at = $2 "
        "WHERE id = $1 AND status = 'pending' RETURNING true", reminder_id, now) is True


async def start_post(conn: Any, reminder_id: int, now: datetime) -> None:
    """Commit before invoking the channel; an unknown result after this point must never retry."""
    await conn.execute("UPDATE reminders SET post_started_at = $2, updated_at = $2 "
                       "WHERE id = $1 AND status = 'sending'", reminder_id, now)


async def release(conn: Any, reminder_id: int, *, now: datetime, error: str | None) -> None:
    """The channel said it did not send it: pending again, to be tried at the next tick."""
    await conn.execute("UPDATE reminders SET status = 'pending', post_started_at = NULL, detail = $2, updated_at = $3 "
                       "WHERE id = $1 AND status = 'sending'", reminder_id, error, now)
