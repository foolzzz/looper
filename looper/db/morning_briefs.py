"""The ``morning_briefs`` table (morning brief RFC): one row per local date and operator direct chat.

``sending`` is claimed and committed before the channel is asked. ``sent`` went out. ``skipped`` had nothing to
say, or there was no operator chat. ``uncertain`` may have gone out (the sender stopped, or the channel's answer
was lost) and is never sent again. ``failed`` is a refusal, tried again while ``attempts`` is under the limit.
The delivery (``agents.morning_brief``) is the only writer. Weekends write nothing. A claim stores the digest
ids that read returned, on the row, in that same write. A sent or uncertain brief copies those ids into
``morning_brief_consumed``, in the same transaction as its status. A sender that stops before then is
``uncertain`` and consumes exactly the stored ids; it does not scan ``proactive_deliveries`` by time. The next
brief reads the first attempt's dates through its own day, except those ids. A refusal records none and clears
the stored ids, so the retry reads the same digests. ``consumed_until`` is that brief's check time, not a
watermark: a row whose insert had not committed is absent from the ids, and a later brief still reads it.
Triage events mirror the digest policy: the claim stores ``triage_ids`` and a sent or uncertain brief records
them in ``morning_brief_triage`` in the same transaction, including recovery. A refusal clears them.
The compatibility column ``briefed_run_id`` is never written here.
Releasing a retry puts that refusal back, attempt count included, and clears the ids this claim stored; only a
fresh claim is deleted.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from ..agents import triage_settings


async def get(conn: Any, local_date: date, channel_id: str, chat: str) -> dict[str, Any] | None:
    row = await conn.fetchrow(
        "SELECT local_date, channel_id, chat, status, attempts, detail FROM morning_briefs "
        "WHERE local_date = $1 AND channel_id = $2 AND chat = $3",
        local_date, channel_id, chat)
    return dict(row) if row is not None else None


async def orphans_uncertain(conn: Any, now: datetime) -> int:
    """Settle as ``uncertain`` what a sender claimed and never settled. Under the delivery lock no claim is live,
    so a ``sending`` row is one whose sender stopped: it may have been sent, and is never sent again.

    Consume exactly the digest ids stored with that claim. Do not scan by ``decided_at``: a row that commits
    after the read is not in the set, and a later brief still reads it."""
    async with conn.transaction():
        claimed = await conn.fetch(
            "SELECT local_date, channel_id, chat, read_ids, triage_ids FROM morning_briefs WHERE status = 'sending'")
        for row in claimed:
            await _record_read(conn, row["channel_id"], row["chat"], list(row["read_ids"] or []))
            await _record_triage(conn, row["local_date"], row["channel_id"], row["chat"],
                                 list(row["triage_ids"] or []))
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
                attempts_limit: int, read_ids: list[int] | None = None,
                triage_ids: list[int] | None = None) -> bool:
    """Take this date's brief for one send (committed before the channel is asked).

    ``read_ids`` are the digest rows this attempt's read returned; ``triage_ids`` are its triage events. They are written by the same statement as
    the claim. Recovery consumes that set and does not read the table again. A refusal under ``attempts_limit``
    is taken again and keeps its detail, so releasing the claim can put that refusal back; anything else is
    left as it is."""
    ids = read_ids or []
    row = await get(conn, local_date, channel_id, chat)
    if row is None:
        return await conn.fetchval(
            "INSERT INTO morning_briefs (local_date, channel_id, chat, status, attempts, updated_at, read_ids, triage_ids) "
            "VALUES ($1, $2, $3, 'sending', 1, $4, $5::bigint[], $6::bigint[]) "
            "ON CONFLICT (local_date, channel_id, chat) DO NOTHING RETURNING true",
            local_date, channel_id, chat, now, ids, triage_ids or []) is True
    if row["status"] == "failed" and row["attempts"] < attempts_limit:
        return await conn.fetchval(
            "UPDATE morning_briefs SET status = 'sending', attempts = attempts + 1, updated_at = $4, "
            "read_ids = $6::bigint[], triage_ids = $7::bigint[] "
            "WHERE local_date = $1 AND channel_id = $2 AND chat = $3 AND status = 'failed' AND attempts < $5 "
            "RETURNING true",
            local_date, channel_id, chat, now, attempts_limit, ids, triage_ids or []) is True
    return False


async def release(conn: Any, local_date: date, channel_id: str, chat: str) -> None:
    """Drop a claim that was not sent, so a later tick can take the day.

    A fresh claim (this date had no row; its attempt count is 1) is deleted. A claim that continued a refusal
    is put back as that refusal, with the attempt count it had before this claim. Deleting it would hide the
    first attempt, and the next day would no longer read that attempt's digests."""
    restored = await conn.fetchval(
        "UPDATE morning_briefs SET status = 'failed', attempts = attempts - 1, read_ids = '{}', triage_ids = '{}' "
        "WHERE local_date = $1 AND channel_id = $2 AND chat = $3 AND status = 'sending' AND attempts > 1 "
        "RETURNING true",
        local_date, channel_id, chat)
    if restored:
        return
    await conn.execute(
        "DELETE FROM morning_briefs WHERE local_date = $1 AND channel_id = $2 AND chat = $3 AND status = 'sending'",
        local_date, channel_id, chat)


async def _record_read(conn: Any, channel_id: str, chat: str, read_ids: list[int]) -> None:
    """Record the digest rows a brief actually read. An id that is not this chat's digest is ignored."""
    if not read_ids:
        return
    triage_filter = "AND source <> 'triage' " if await triage_settings.enabled(db=conn) else ""
    await conn.execute(
        "INSERT INTO morning_brief_consumed (delivery_id) "
        "SELECT id FROM proactive_deliveries "
        "WHERE id = ANY($1::bigint[]) AND decision = 'digest' AND channel_id = $2 AND chat = $3 "
        "AND source <> 'morning_brief' "
        + triage_filter + "ON CONFLICT DO NOTHING",
        read_ids, channel_id, chat)


async def _record_triage(conn: Any, local_date: date, channel_id: str, chat: str, triage_ids: list[int]) -> None:
    """Record exactly the events read; a source deleted during the send is ignored."""
    if not triage_ids:
        return
    await conn.execute(
        "INSERT INTO morning_brief_triage (event_id, local_date, channel_id, chat) "
        "SELECT id, $2, $3, $4 FROM triage_events WHERE id = ANY($1::bigint[]) "
        "ON CONFLICT DO NOTHING",
        triage_ids, local_date, channel_id, chat)


async def settle(conn: Any, local_date: date, channel_id: str, chat: str, status: str, *, now: datetime,
                 detail: str | None = None, consumed_until: datetime | None = None,
                 read_ids: list[int] | None = None, triage_ids: list[int] | None = None) -> bool:
    """Move a claimed brief to ``status``. ``consumed_until`` is the check's time when ``read_ids`` were taken
    (unset on a refusal, so the retry reads them again). Those ids are written in this same transaction. A
    refusal clears the ids the claim stored and records none. False when it was no longer ``sending``."""
    if status == "failed":
        read_ids, triage_ids = None, None
    async with conn.transaction():
        applied = await conn.fetchval(
            "UPDATE morning_briefs SET status = $4, detail = $5, updated_at = $6, "
            "consumed_until = COALESCE($7, consumed_until), "
            "read_ids = COALESCE($8::bigint[], '{}'::bigint[]), "
            "triage_ids = COALESCE($9::bigint[], '{}'::bigint[]) "
            "WHERE local_date = $1 AND channel_id = $2 AND chat = $3 AND status = 'sending' RETURNING true",
            local_date, channel_id, chat, status, detail, now, consumed_until, read_ids, triage_ids)
        if applied and read_ids is not None:
            await _record_read(conn, channel_id, chat, read_ids)
        if applied and triage_ids is not None:
            await _record_triage(conn, local_date, channel_id, chat, triage_ids)
    return applied is True


async def reminders_today(conn: Any, *, channel_id: str, chat: str, start: datetime, end: datetime
                          ) -> list[dict[str, Any]]:
    """Pending reminders whose ``due_at`` falls on the half-open local day ``[start, end)``, soonest first."""
    rows = await conn.fetch(
        "SELECT id, text, due_at FROM reminders "
        "WHERE status = 'pending' AND channel_id = $1 AND chat = $2 AND due_at >= $3 AND due_at < $4 "
        "ORDER BY due_at, id",
        channel_id, chat, start, end)
    return [dict(row) for row in rows]


async def first_attempt_date(conn: Any, channel_id: str, chat: str) -> date | None:
    """The local date of this chat's earliest brief, including a refusal. None before the first attempt."""
    return await conn.fetchval(
        "SELECT min(local_date) FROM morning_briefs WHERE channel_id = $1 AND chat = $2", channel_id, chat)


async def digests(conn: Any, *, channel_id: str, chat: str, until: datetime,
                  dates: list[date] | None = None) -> list[dict[str, Any]]:
    """Proactive messages deferred for this chat, oldest first, decided at or before ``until``, whose
    ``digest_date`` is in ``dates`` (the first attempt's dates through the day being sent). A row already recorded
    in ``morning_brief_consumed`` is not read again. A morning brief is not one of them. ``source`` is what the
    metrics call the send point. There is no lower bound on ``decided_at``: a row that commits after this read,
    with an earlier ``decided_at``, is still here next time."""
    from ..agents import leads_model

    triage_filter = "AND source <> 'triage' " if await triage_settings.enabled(db=conn) else ""
    rules_on = await leads_model.rules_enabled(db=conn)
    # Disabled execution never reads a rule table. Its committed notices must also
    # stay deferred here, rather than escaping through the independent brief switch.
    rule_filter = ("" if rules_on else "AND NOT (source='intent' AND COALESCE(source_ref,'') "
                   "~ '^rule-(created|discovery|fault):[0-9]+$') ")
    rows = await conn.fetch(
        "SELECT id, source AS kind, reason, decided_at, task_id, schedule_id, source_ref FROM proactive_deliveries "
        "WHERE decision = 'digest' AND channel_id = $1 AND chat = $2 AND source <> 'morning_brief' "
        + triage_filter + rule_filter + "AND decided_at <= $3 AND digest_date = ANY($4::date[]) "
        "AND NOT EXISTS (SELECT 1 FROM morning_brief_consumed c WHERE c.delivery_id = proactive_deliveries.id) "
        "ORDER BY decided_at, id",
        channel_id, chat, until, dates or [])
    result = [dict(row) for row in rows]
    return await filter_rule_digests(conn, result, channel_id=channel_id, chat=chat) if rules_on else result


async def filter_rule_digests(conn: Any, rows: list[dict[str, Any]], *, channel_id: str,
                              chat: str) -> list[dict[str, Any]]:
    """Recheck the exact previously read rows immediately before a brief is posted."""
    from ..agents.intent_rule_delivery import (confirmation_allowed, is_rule_reference, notice_context,
                                                notice_target, notice_visible)
    from ..agents import leads_model

    rules_on = await leads_model.rules_enabled(db=conn)
    kept: list[dict[str, Any]] = []
    for row in rows:
        ref = row.get('source_ref')
        if row['kind'] != 'intent' or not ref:
            kept.append(row)
        elif is_rule_reference(ref):
            if not rules_on:
                continue
            context = await notice_context(conn, ref)
            if (context and context['state'] == 'digest' and await notice_visible(conn, context)
                    and await notice_target(conn, context) == (channel_id, chat)):
                kept.append({**row, 'rule_notice': context})
        elif rules_on and ref.isdecimal():
            intent = await conn.fetchrow('SELECT id,state,merged_into FROM intents WHERE id=$1', int(ref))
            if intent is None or not await confirmation_allowed(conn, intent['id'], rules_on=True):
                continue
            if intent['state'] == 'pending':
                kept.append(row)
            elif (intent['state'] == 'merged' and intent['merged_into'] is not None
                  and await confirmation_allowed(conn, intent['merged_into'], rules_on=True)):
                kept.append(row)
        else:
            kept.append(row)
    return kept
