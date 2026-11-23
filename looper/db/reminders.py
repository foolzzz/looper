"""The ``reminders`` table (reminders RFC): the user's one-time reminders and what became of each.

A reminder is ``pending`` until it is due. Only the delivery (``agents.reminders.ReminderService``) moves it on: it
claims one as ``sending`` (committed before the channel is asked) and settles it ``delivered``, ``failed`` or
``uncertain``, or settles one Looper could not send on time as ``missed``. Only the user cancels one
(``cancelled``), and only while it is ``pending``.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from .connection import get_pool

STATUSES = ("pending", "sending", "delivered", "failed", "uncertain", "missed", "cancelled")
LIST_DEFAULT, LIST_MAX = 20, 50


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


async def cancel(reminder_id: int, *, now: datetime, db: Any = None) -> tuple[dict[str, Any] | None, bool]:
    """Cancel a pending reminder: (the reminder as it now is, whether this call cancelled it); (None, False) when
    there is none."""
    pool = _db(db)
    row = await pool.fetchrow(
        "UPDATE reminders SET status = 'cancelled', detail = 'cancelled by the user', settled_at = $2, "
        "updated_at = $2 WHERE id = $1 AND status = 'pending' RETURNING *", reminder_id, now)
    if row is not None:
        return dict(row), True
    return await get(reminder_id, db=pool), False


# The delivery's steps; each runs on the connection that holds the delivery's lock.

async def orphans_uncertain(conn: Any, now: datetime) -> int:
    """Settle as ``uncertain`` what a sender claimed and never settled (it stopped mid-send): it may have been sent,
    so it is never sent again. Only the lock's holder sends, so under the lock no claim is a live one."""
    done = await conn.execute(
        "UPDATE reminders SET status = 'uncertain', detail = 'the sender stopped before recording the answer', "
        "settled_at = $1, updated_at = $1 WHERE status = 'sending'", now)
    return int(done.split()[-1])


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
        "UPDATE reminders SET status = 'sending', attempts = attempts + 1, updated_at = $2 "
        "WHERE id = $1 AND status = 'pending' RETURNING true", reminder_id, now) is True


async def release(conn: Any, reminder_id: int, *, now: datetime, error: str | None) -> None:
    """The channel said it did not send it: pending again, to be tried at the next tick."""
    await conn.execute("UPDATE reminders SET status = 'pending', detail = $2, updated_at = $3 "
                       "WHERE id = $1 AND status = 'sending'", reminder_id, error, now)
