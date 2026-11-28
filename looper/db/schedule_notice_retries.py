"""One episode of one alert kind of one schedule.

The key is the schedule, the alert family (``kind``), the episode and the role (``alert`` or ``recovery``).
Episode numbers increase when that kind's failure period opens and are not reused. One
``UPDATE ... WHERE state = 'pending'`` is the claim: a second caller updates nothing and does not send.
A completion notice is not a row here.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any, AsyncIterator

import asyncpg

Db = asyncpg.Pool | asyncpg.Connection

_TERMINAL = ("sent", "uncertain", "gave_up", "superseded")


@asynccontextmanager
async def _transaction(db: Db) -> AsyncIterator[asyncpg.Connection]:
    if isinstance(db, asyncpg.Pool):
        async with db.acquire() as conn:
            async with conn.transaction():
                yield conn
    else:
        async with db.transaction():
            yield db


async def open_alert(db: Db, *, schedule_id: str, kind: str, run_id: str | None, text: str, channel: str | None,
                     chat: str | None, failures: int | None, now: datetime) -> Any | None:
    """Open an alert as ``pending``, due now. Supersede a pending alert or recovery of this kind.

    A recovery that was recorded and not yet claimed, whose episode has no alert row, is that same failure
    period: the alert joins it and that recovery stays. Any other open is the next episode. None when the
    schedule is already gone.
    """
    async with _transaction(db) as conn:
        if await conn.fetchval("SELECT id FROM schedules WHERE id = $1 FOR UPDATE", schedule_id) is None:
            return None
        latest = await conn.fetchval(
            "SELECT COALESCE(MAX(episode), 0) FROM schedule_notice_retries "
            "WHERE schedule_id = $1 AND kind = $2", schedule_id, kind)
        attached = None
        if latest:
            recovery = await conn.fetchrow(
                "SELECT * FROM schedule_notice_retries WHERE schedule_id = $1 AND kind = $2 AND episode = $3 "
                "AND role = 'recovery' FOR UPDATE", schedule_id, kind, latest)
            has_alert = await conn.fetchval(
                "SELECT EXISTS (SELECT 1 FROM schedule_notice_retries WHERE schedule_id = $1 AND kind = $2 "
                "AND episode = $3 AND role = 'alert')", schedule_id, kind, latest)
            if (recovery is not None and not has_alert and recovery["state"] == "pending"
                    and recovery["retries"] == 0 and recovery["error"] is None):
                attached = recovery
        if attached is not None:
            episode = attached["episode"]
            await conn.execute(
                "UPDATE schedule_notice_retries SET state = 'superseded', next_attempt_at = NULL, updated_at = $3, "
                "error = COALESCE(error, 'a new failure period started') "
                "WHERE schedule_id = $1 AND kind = $2 AND state = 'pending' AND id <> $4",
                schedule_id, kind, now, attached["id"])
        else:
            await conn.execute(
                "UPDATE schedule_notice_retries SET state = 'superseded', next_attempt_at = NULL, updated_at = $3, "
                "error = COALESCE(error, 'a new failure period started') "
                "WHERE schedule_id = $1 AND kind = $2 AND state = 'pending'",
                schedule_id, kind, now)
            episode = latest + 1
        return await conn.fetchrow(
            "INSERT INTO schedule_notice_retries (schedule_id, kind, episode, role, run_id, state, retries, "
            "next_attempt_at, failures, text, channel, chat, created_at, updated_at) "
            "VALUES ($1, $2, $3, 'alert', $4, 'pending', 0, $5, $6, $7, $8, $9, $5, $5) RETURNING *",
            schedule_id, kind, episode, run_id, now, failures, text, channel, chat)


async def open_recovery(db: Db, *, schedule_id: str, kind: str, run_id: str | None, text: str, channel: str | None,
                        chat: str | None, now: datetime) -> Any | None:
    """Open the latest episode's recovery as ``pending``, due now, and supersede that episode's pending alert.

    The text stored here is the plain recovery. The supplement is chosen later, when the row is claimed. None when
    the schedule is gone, or when this episode's recovery has already left ``pending``. A recovery that is still
    pending is made due now and returned.
    """
    async with _transaction(db) as conn:
        if await conn.fetchval("SELECT id FROM schedules WHERE id = $1 FOR UPDATE", schedule_id) is None:
            return None
        latest = await conn.fetchval(
            "SELECT COALESCE(MAX(episode), 0) FROM schedule_notice_retries WHERE schedule_id = $1 AND kind = $2",
            schedule_id, kind)
        if latest:
            recovery = await conn.fetchrow(
                "SELECT * FROM schedule_notice_retries WHERE schedule_id = $1 AND kind = $2 AND episode = $3 "
                "AND role = 'recovery' FOR UPDATE", schedule_id, kind, latest)
            if recovery is not None:
                if recovery["state"] != "pending":
                    return None
                return await conn.fetchrow(
                    "UPDATE schedule_notice_retries SET next_attempt_at = $2, updated_at = $2, text = $3, "
                    "run_id = $4, channel = $5, chat = $6 WHERE id = $1 RETURNING *",
                    recovery["id"], now, text, run_id, channel, chat)
            await conn.execute(
                "UPDATE schedule_notice_retries SET state = 'superseded', next_attempt_at = NULL, updated_at = $4, "
                "error = COALESCE(error, 'the schedule recovered before this alert was sent') "
                "WHERE schedule_id = $1 AND kind = $2 AND episode = $3 AND role = 'alert' AND state = 'pending'",
                schedule_id, kind, latest, now)
            episode = latest
        else:
            episode = 1
        return await conn.fetchrow(
            "INSERT INTO schedule_notice_retries (schedule_id, kind, episode, role, run_id, state, retries, "
            "next_attempt_at, text, channel, chat, created_at, updated_at) "
            "VALUES ($1, $2, $3, 'recovery', $4, 'pending', 0, $5, $6, $7, $8, $5, $5) RETURNING *",
            schedule_id, kind, episode, run_id, now, text, channel, chat)


async def due(conn: Db, now: datetime) -> list[Any]:
    """Due ``pending`` rows, alerts before recoveries, so one pass finishes an alert before claiming its recovery."""
    return await conn.fetch(
        "SELECT * FROM schedule_notice_retries WHERE state = 'pending' AND next_attempt_at <= $1 "
        "ORDER BY role, id", now)


async def claim_for_send(db: Db, retry_id: int, now: datetime) -> tuple[Any, Any | None] | None:
    """Move a due ``pending`` row to ``sending``. None when it is no longer claimable.

    The first claim does not count a retry. A later one does, once a previous attempt has failed. A recovery whose
    alert is still ``sending`` stays ``pending`` and is not counted: the next pass tries again. The returned alert
    row, when there is one, is locked with the claim so the supplement matches it.
    """
    async with _transaction(db) as conn:
        row = await conn.fetchrow(
            "SELECT * FROM schedule_notice_retries WHERE id = $1 FOR UPDATE", retry_id)
        if row is None or row["state"] != "pending":
            return None
        alert = None
        if row["role"] == "recovery":
            alert = await conn.fetchrow(
                "SELECT * FROM schedule_notice_retries WHERE schedule_id = $1 AND kind = $2 AND episode = $3 "
                "AND role = 'alert' FOR UPDATE", row["schedule_id"], row["kind"], row["episode"])
            if alert is not None and alert["state"] == "sending":
                await conn.execute(
                    "UPDATE schedule_notice_retries SET next_attempt_at = $2, updated_at = $2 WHERE id = $1",
                    retry_id, now)
                return None
        retries = row["retries"] + (1 if row["retries"] or row["error"] is not None else 0)
        claimed = await conn.fetchrow(
            "UPDATE schedule_notice_retries SET state = 'sending', retries = $2, next_attempt_at = NULL, "
            "updated_at = $3 WHERE id = $1 AND state = 'pending' AND next_attempt_at <= $3 RETURNING *",
            retry_id, retries, now)
        if claimed is None:
            return None
        return claimed, alert


async def finish_send(db: Db, retry_id: int, *, outcome: str, error: str | None, now: datetime,
                      rejected_state: str, rejected_at: datetime | None) -> str:
    """Settle a ``sending`` row. A missing row was deleted with its schedule and is ``superseded``.

    ``sent`` and ``unknown`` land as ``sent`` and ``uncertain``. A refusal is ``rejected_state`` unless this
    episode's alert already has a recovery, or a later episode of this kind exists: then it is ``superseded``
    and is not queued again.
    """
    async with _transaction(db) as conn:
        row = await conn.fetchrow(
            "SELECT * FROM schedule_notice_retries WHERE id = $1 FOR UPDATE", retry_id)
        if row is None:
            return "superseded"
        if row["state"] != "sending":
            return row["state"]
        if outcome == "sent":
            state, nxt, err = "sent", None, None
        elif outcome == "unknown":
            state, nxt, err = "uncertain", None, error
        else:
            higher = await conn.fetchval(
                "SELECT EXISTS (SELECT 1 FROM schedule_notice_retries WHERE schedule_id = $1 AND kind = $2 "
                "AND episode > $3)", row["schedule_id"], row["kind"], row["episode"])
            closed = False
            if row["role"] == "alert":
                closed = await conn.fetchval(
                    "SELECT EXISTS (SELECT 1 FROM schedule_notice_retries WHERE schedule_id = $1 AND kind = $2 "
                    "AND episode = $3 AND role = 'recovery')",
                    row["schedule_id"], row["kind"], row["episode"])
            if higher or closed:
                state, nxt, err = "superseded", None, error
            else:
                state, nxt, err = rejected_state, rejected_at, error
        await conn.execute(
            "UPDATE schedule_notice_retries SET state = $2, error = $3, next_attempt_at = $4, updated_at = $5 "
            "WHERE id = $1", retry_id, state, err, nxt, now)
        return state


async def give_up(conn: Db, retry_id: int, now: datetime, error: str) -> bool:
    """Stop a ``pending`` retry without sending. False when it was no longer pending, including when it was deleted."""
    return await conn.fetchrow(
        "UPDATE schedule_notice_retries SET state = 'gave_up', next_attempt_at = NULL, error = $3, updated_at = $2 "
        "WHERE id = $1 AND state = 'pending' RETURNING id", retry_id, now, error) is not None


async def settle_if_sending(conn: Db, retry_id: int, *, state: str, error: str, now: datetime) -> bool:
    """Settle a claim that is still ``sending``. False when that send already finished or the row is gone."""
    return await conn.fetchrow(
        "UPDATE schedule_notice_retries SET state = $2, next_attempt_at = NULL, error = $3, updated_at = $4 "
        "WHERE id = $1 AND state = 'sending' RETURNING id", retry_id, state, error, now) is not None


async def note_failure_count(conn: Db, schedule_id: str, failures: int) -> None:
    """The open failure alert's count is the streak at the latest failure, not the count when it was opened."""
    await conn.execute(
        "UPDATE schedule_notice_retries SET failures = $2 "
        "WHERE schedule_id = $1 AND kind = 'failures' AND role = 'alert' "
        "AND episode = (SELECT MAX(episode) FROM schedule_notice_retries "
        "WHERE schedule_id = $1 AND kind = 'failures') "
        "AND state <> 'superseded' AND (failures IS NULL OR failures < $2)",
        schedule_id, failures)


async def settle_stopped(conn: Db, now: datetime, error: str) -> None:
    """A claim left ``sending`` did not finish. It may have gone: uncertain, and never sent again.

    ``pending`` stays pending. A recovery that was waiting can still name the alert, which may not have been
    delivered.
    """
    await conn.execute(
        "UPDATE schedule_notice_retries SET state = 'uncertain', next_attempt_at = NULL, error = $2, updated_at = $1 "
        "WHERE state = 'sending'", now, error)


async def prune_settled(conn: Db, before: datetime, limit: int) -> None:
    """Drop settled retry rows older than ``before``. ``pending`` and ``sending`` stay."""
    await conn.execute(
        "DELETE FROM schedule_notice_retries WHERE id IN (SELECT id FROM schedule_notice_retries "
        "WHERE state = ANY($3::text[]) AND created_at < $1 LIMIT $2)", before, limit, list(_TERMINAL))
