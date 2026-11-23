"""The ``morning_briefs`` table (morning brief RFC): one row per local date and operator direct chat.

``sending`` is claimed and committed before the channel is asked. ``sent`` went out. ``skipped`` had nothing to
say, or there was no operator chat. ``uncertain`` may have gone out (the sender stopped, or the channel's answer
was lost) and is never sent again. ``failed`` is a refusal, tried again while ``attempts`` is under the limit.
The delivery (``agents.morning_brief``) is the only writer. Weekends write nothing. ``consumed_until`` is how far
that chat's briefs have read: a sent, skipped or uncertain brief sets it to the check's time, and the next brief
reads digests decided after it. A refusal leaves it unset, so the retry reads the same digests. Until a boundary
is recorded, a later day still starts from the first attempt's date.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any


async def get(conn: Any, local_date: date, channel_id: str, chat: str) -> dict[str, Any] | None:
    row = await conn.fetchrow(
        "SELECT local_date, channel_id, chat, status, attempts, detail FROM morning_briefs "
        "WHERE local_date = $1 AND channel_id = $2 AND chat = $3",
        local_date, channel_id, chat)
    return dict(row) if row is not None else None


async def orphans_uncertain(conn: Any, now: datetime) -> int:
    """Settle as ``uncertain`` what a sender claimed and never settled. Under the delivery lock no claim is live,
    so a ``sending`` row is one whose sender stopped: it may have been sent, and is never sent again."""
    done = await conn.execute(
        "UPDATE morning_briefs SET status = 'uncertain', "
        "detail = 'the sender stopped before recording the answer', "
        "consumed_until = COALESCE(consumed_until, updated_at), updated_at = $1 WHERE status = 'sending'",
        now)
    return int(done.split()[-1])


async def skip(conn: Any, local_date: date, channel_id: str, chat: str, now: datetime, detail: str | None) -> None:
    """Record that this date sends nothing. A refusal that no longer has anything to say becomes ``skipped``;
    a row already sent, skipped or uncertain stays as it is."""
    await conn.execute(
        "INSERT INTO morning_briefs (local_date, channel_id, chat, status, attempts, detail, updated_at, "
        "consumed_until) VALUES ($1, $2, $3, 'skipped', 0, $4, $5, $5) "
        "ON CONFLICT (local_date, channel_id, chat) DO UPDATE "
        "SET status = 'skipped', detail = EXCLUDED.detail, updated_at = EXCLUDED.updated_at, "
        "consumed_until = EXCLUDED.consumed_until WHERE morning_briefs.status = 'failed'",
        local_date, channel_id, chat, detail, now)


async def claim(conn: Any, local_date: date, channel_id: str, chat: str, now: datetime, *,
                attempts_limit: int) -> bool:
    """Take this date's brief for one send (committed before the channel is asked). A refusal under
    ``attempts_limit`` is taken again; anything else is left as it is."""
    row = await get(conn, local_date, channel_id, chat)
    if row is None:
        return await conn.fetchval(
            "INSERT INTO morning_briefs (local_date, channel_id, chat, status, attempts, updated_at) "
            "VALUES ($1, $2, $3, 'sending', 1, $4) "
            "ON CONFLICT (local_date, channel_id, chat) DO NOTHING RETURNING true",
            local_date, channel_id, chat, now) is True
    if row["status"] == "failed" and row["attempts"] < attempts_limit:
        return await conn.fetchval(
            "UPDATE morning_briefs SET status = 'sending', attempts = attempts + 1, detail = NULL, updated_at = $4 "
            "WHERE local_date = $1 AND channel_id = $2 AND chat = $3 AND status = 'failed' AND attempts < $5 "
            "RETURNING true",
            local_date, channel_id, chat, now, attempts_limit) is True
    return False


async def release(conn: Any, local_date: date, channel_id: str, chat: str) -> None:
    """Drop a claim that was not sent, so a later tick can take the day."""
    await conn.execute(
        "DELETE FROM morning_briefs WHERE local_date = $1 AND channel_id = $2 AND chat = $3 AND status = 'sending'",
        local_date, channel_id, chat)


async def settle(conn: Any, local_date: date, channel_id: str, chat: str, status: str, *, now: datetime,
                 detail: str | None = None, consumed_until: datetime | None = None) -> bool:
    """Move a claimed brief to ``status``. ``consumed_until`` is the check's time when the digests were taken
    (unset on a refusal, so the retry reads them again). False when it was no longer ``sending``."""
    return await conn.fetchval(
        "UPDATE morning_briefs SET status = $4, detail = $5, updated_at = $6, "
        "consumed_until = COALESCE($7, consumed_until) "
        "WHERE local_date = $1 AND channel_id = $2 AND chat = $3 AND status = 'sending' RETURNING true",
        local_date, channel_id, chat, status, detail, now, consumed_until) is True


async def reminders_today(conn: Any, *, channel_id: str, chat: str, start: datetime, end: datetime
                          ) -> list[dict[str, Any]]:
    """Pending reminders whose ``due_at`` falls on the half-open local day ``[start, end)``, soonest first."""
    rows = await conn.fetch(
        "SELECT id, text, due_at FROM reminders "
        "WHERE status = 'pending' AND channel_id = $1 AND chat = $2 AND due_at >= $3 AND due_at < $4 "
        "ORDER BY due_at, id",
        channel_id, chat, start, end)
    return [dict(row) for row in rows]


async def consumption_boundary(conn: Any, channel_id: str, chat: str) -> datetime | None:
    """How far this chat's briefs have read. None until one has been sent, skipped or left uncertain."""
    return await conn.fetchval(
        "SELECT max(consumed_until) FROM morning_briefs WHERE channel_id = $1 AND chat = $2", channel_id, chat)


async def first_attempt_date(conn: Any, channel_id: str, chat: str) -> date | None:
    """The local date of this chat's earliest brief, including a refusal. None before the first attempt."""
    return await conn.fetchval(
        "SELECT min(local_date) FROM morning_briefs WHERE channel_id = $1 AND chat = $2", channel_id, chat)


async def digests(conn: Any, *, channel_id: str, chat: str, until: datetime, dates: list[date] | None = None,
                  after: datetime | None = None) -> list[dict[str, Any]]:
    """Proactive messages deferred for this chat, oldest first, decided at or before ``until``. With ``after``
    (the previous brief's boundary) that is every one decided since; without it, those whose ``digest_date`` is in
    ``dates`` (a chat that has not recorded a boundary: its first attempt's dates through the day being sent). A
    morning brief is not one of them. ``source`` is what the metrics call the send point."""
    if after is not None:
        rows = await conn.fetch(
            "SELECT id, source AS kind, reason, decided_at, task_id, schedule_id FROM proactive_deliveries "
            "WHERE decision = 'digest' AND channel_id = $1 AND chat = $2 AND source <> 'morning_brief' "
            "AND decided_at > $3 AND decided_at <= $4 ORDER BY decided_at, id",
            channel_id, chat, after, until)
    else:
        rows = await conn.fetch(
            "SELECT id, source AS kind, reason, decided_at, task_id, schedule_id FROM proactive_deliveries "
            "WHERE decision = 'digest' AND channel_id = $1 AND chat = $2 AND digest_date = ANY($3::date[]) "
            "AND source <> 'morning_brief' AND decided_at <= $4 ORDER BY decided_at, id",
            channel_id, chat, dates or [], until)
    return [dict(row) for row in rows]
