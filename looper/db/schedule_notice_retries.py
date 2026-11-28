"""Claim rows for a schedule notice that a channel refused or may have accepted.

The unique key is the schedule, the kind and the run (``run_key`` is ``''`` when there is no run). One
``UPDATE ... WHERE state = 'pending'`` is the claim: a second caller updates nothing and does not send.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

import asyncpg

Db = asyncpg.Pool | asyncpg.Connection

_TERMINAL = ("sent", "uncertain", "gave_up")
_UNREPORTED = ("pending", "gave_up", "uncertain")


async def held(conn: Db, schedule_id: str, kind: str, run_key: str) -> str | None:
    """``sent`` or ``failed`` when this notice must not be sent again, else None (send, or re-arm a later streak)."""
    row = await conn.fetchrow(
        "SELECT state FROM schedule_notice_retries WHERE schedule_id = $1 AND kind = $2 AND run_key = $3",
        schedule_id, kind, run_key)
    if row is None:
        return None
    if row["state"] in ("pending", "sending") or kind == "run":
        return "sent" if row["state"] == "sent" else "failed"
    return None


async def begin_send(conn: Db, *, schedule_id: str, kind: str, run_id: str | None, text: str, channel: str | None,
                     chat: str | None, failures: int | None, summary_of: list[int] | None, now: datetime,
                     ) -> Any | None:
    """Claim the row as ``sending`` before the send. None when another caller already holds this notice."""
    run_key = run_id or ""
    row = await conn.fetchrow(
        "INSERT INTO schedule_notice_retries (schedule_id, kind, run_id, run_key, state, retries, text, channel, "
        "chat, failures, summary_of, created_at, updated_at) VALUES ($1, $2, $3, $4, 'sending', 0, $5, $6, $7, $8, "
        "$9::bigint[], $10, $10) ON CONFLICT (schedule_id, kind, run_key) DO NOTHING RETURNING *",
        schedule_id, kind, run_id, run_key, text, channel, chat, failures, summary_of, now)
    if row is not None or kind == "run":
        return row
    return await conn.fetchrow(
        "UPDATE schedule_notice_retries SET state = 'sending', retries = 0, next_attempt_at = NULL, error = NULL, "
        "text = $4, channel = $5, chat = $6, failures = $7, summary_of = $8::bigint[], run_id = $9, "
        "reported_at = NULL, created_at = $10, updated_at = $10 WHERE schedule_id = $1 AND kind = $2 "
        "AND run_key = $3 AND state = ANY($11::text[]) RETURNING *",
        schedule_id, kind, run_key, text, channel, chat, failures, summary_of, run_id, now, list(_TERMINAL))


async def due(conn: Db, now: datetime) -> list[Any]:
    return await conn.fetch(
        "SELECT * FROM schedule_notice_retries WHERE state = 'pending' AND next_attempt_at <= $1 ORDER BY id", now)


async def claim(conn: Db, retry_id: int, now: datetime) -> Any | None:
    """Move a due ``pending`` row to ``sending`` and count the attempt. None when another pass took it."""
    return await conn.fetchrow(
        "UPDATE schedule_notice_retries SET state = 'sending', retries = retries + 1, next_attempt_at = NULL, "
        "updated_at = $2 WHERE id = $1 AND state = 'pending' AND next_attempt_at <= $2 RETURNING *", retry_id, now)


async def give_up(conn: Db, retry_id: int, now: datetime, error: str) -> bool:
    """Stop a ``pending`` retry without sending. False when it was no longer pending."""
    return await conn.fetchrow(
        "UPDATE schedule_notice_retries SET state = 'gave_up', next_attempt_at = NULL, error = $3, updated_at = $2 "
        "WHERE id = $1 AND state = 'pending' RETURNING id", retry_id, now, error) is not None


async def settle(conn: Db, retry_id: int, *, state: str, error: str | None, next_at: datetime | None,
                 now: datetime) -> None:
    await conn.execute(
        "UPDATE schedule_notice_retries SET state = $2, error = $3, next_attempt_at = $4, updated_at = $5 "
        "WHERE id = $1", retry_id, state, error, next_at, now)


async def delete_retry(conn: Db, retry_id: int) -> None:
    await conn.execute("DELETE FROM schedule_notice_retries WHERE id = $1", retry_id)


async def drop_pending_recoveries(conn: Db, schedule_id: str, now: datetime) -> None:
    """A new failure streak starts: do not send a recovery that is still waiting."""
    await conn.execute(
        "UPDATE schedule_notice_retries SET state = 'gave_up', next_attempt_at = NULL, updated_at = $2, "
        "error = COALESCE(error, 'a new failure started before this recovery was sent') "
        "WHERE schedule_id = $1 AND kind = 'failures_recovered' AND state = 'pending'", schedule_id, now)


async def unreported_failure_alerts(conn: Db, schedule_id: str) -> list[Any]:
    return await conn.fetch(
        "SELECT * FROM schedule_notice_retries WHERE schedule_id = $1 AND kind = 'failures_alert' "
        "AND reported_at IS NULL AND state = ANY($2::text[]) ORDER BY id", schedule_id, list(_UNREPORTED))


async def fold_pending(conn: Db, retry_ids: list[int], now: datetime) -> None:
    """The recovery's text names these alerts, so they are not sent on their own. ``reported_at`` stays null until
    that recovery is sent or may have been sent."""
    if not retry_ids:
        return
    await conn.execute(
        "UPDATE schedule_notice_retries SET state = 'gave_up', next_attempt_at = NULL, updated_at = $2 "
        "WHERE id = ANY($1::bigint[]) AND state = 'pending'", retry_ids, now)


async def mark_reported(conn: Db, retry_ids: list[int], now: datetime) -> None:
    if not retry_ids:
        return
    await conn.execute(
        "UPDATE schedule_notice_retries SET reported_at = $2, updated_at = $2 "
        "WHERE id = ANY($1::bigint[]) AND reported_at IS NULL", retry_ids, now)


def summary_ids(row: Any) -> list[int]:
    return [int(item) for item in (row["summary_of"] or ())]


async def settle_stopped(conn: Db, now: datetime, error: str) -> None:
    """A claim left ``sending`` did not finish. It may have gone: uncertain, and never sent again. A recovery that
    was in flight may have named its alerts, so those are not later described as never delivered."""
    rows = await conn.fetch(
        "SELECT summary_of FROM schedule_notice_retries WHERE state = 'sending' AND kind = 'failures_recovered' "
        "AND summary_of IS NOT NULL")
    await mark_reported(conn, [int(item) for row in rows for item in (row["summary_of"] or ())], now)
    await conn.execute(
        "UPDATE schedule_notice_retries SET state = 'uncertain', next_attempt_at = NULL, error = $2, updated_at = $1 "
        "WHERE state = 'sending'", now, error)


async def prune_settled(conn: Db, before: datetime, limit: int) -> None:
    """Drop settled retry rows older than ``before``. ``pending`` and ``sending`` stay."""
    await conn.execute(
        "DELETE FROM schedule_notice_retries WHERE id IN (SELECT id FROM schedule_notice_retries "
        "WHERE state = ANY($3::text[]) AND created_at < $1 LIMIT $2)", before, limit, list(_TERMINAL))
