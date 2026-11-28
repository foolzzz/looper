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
# Alerts a recovery may still have to tell the chat about. ``sending`` counts: the recovery can be written while
# the alert is in flight. ``sent`` does not: that try was delivered.
_UNREPORTED = ("pending", "sending", "gave_up", "uncertain")


async def held(conn: Db, schedule_id: str, kind: str, run_key: str) -> str | None:
    """``sent`` when a run notice was delivered, ``failed`` when it must not be sent, else None.

    A run notice still ``pending`` is None: its completion replay sends it again. ``sending``, ``uncertain`` and
    ``gave_up`` are ``failed``. Any other kind may re-arm a terminal row for a later streak.
    """
    row = await conn.fetchrow(
        "SELECT state FROM schedule_notice_retries WHERE schedule_id = $1 AND kind = $2 AND run_key = $3",
        schedule_id, kind, run_key)
    if row is None:
        return None
    if kind == "run":
        if row["state"] == "sent":
            return "sent"
        if row["state"] == "pending":
            return None
        return "failed"
    if row["state"] in ("pending", "sending"):
        return "failed"
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
    if row is not None:
        return row
    if kind == "run":
        # The completion replay of a refused run. The attempt count and the clock stay, so the retry budget does not
        # move; a row that is already uncertain, gave up, or in flight is left alone.
        return await conn.fetchrow(
            "UPDATE schedule_notice_retries SET state = 'sending', next_attempt_at = NULL, error = NULL, "
            "text = $4, channel = $5, chat = $6, updated_at = $7 "
            "WHERE schedule_id = $1 AND kind = $2 AND run_key = $3 AND state = 'pending' RETURNING *",
            schedule_id, kind, run_key, text, channel, chat, now)
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


async def note_failure_count(conn: Db, schedule_id: str, failures: int) -> None:
    """The open failure alert's count is the streak at the latest failure, not the count when it was opened."""
    await conn.execute(
        "UPDATE schedule_notice_retries SET failures = $2 "
        "WHERE schedule_id = $1 AND kind = 'failures_alert' AND reported_at IS NULL "
        "AND state = ANY($3::text[]) AND (failures IS NULL OR failures < $2)",
        schedule_id, failures, list(_UNREPORTED))


async def named_in_recovery(conn: Db, alert_id: int) -> bool:
    """Whether a recovery already names this alert, so a later refusal is not queued again.

    A recovery that was delivered is deleted, and the alert's ``reported_at`` is what remains. A recovery that is
    still only written keeps the id in ``summary_of``.
    """
    if await conn.fetchval("SELECT reported_at IS NOT NULL FROM schedule_notice_retries WHERE id = $1", alert_id):
        return True
    return await conn.fetchval(
        "SELECT EXISTS (SELECT 1 FROM schedule_notice_retries "
        "WHERE kind = 'failures_recovered' AND summary_of @> ARRAY[$1]::bigint[])", alert_id) is True


async def settle_if_sending(conn: Db, retry_id: int, *, state: str, error: str, now: datetime) -> bool:
    """Settle a claim that is still ``sending``. False when that send already finished."""
    return await conn.fetchrow(
        "UPDATE schedule_notice_retries SET state = $2, next_attempt_at = NULL, error = $3, updated_at = $4 "
        "WHERE id = $1 AND state = 'sending' RETURNING id", retry_id, state, error, now) is not None


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
