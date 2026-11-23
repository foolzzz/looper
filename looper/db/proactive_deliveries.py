"""``proactive_deliveries``: one row per proactive message. The metrics write a channel's receipt after sending
(``insert``); with the delivery layer on, ``agents/proactive_delivery.decide`` writes the decision first
(``insert_decision``) and the send point its receipt (``record_receipt``), all under the message's ``dedupe_key``."""
from __future__ import annotations

from collections.abc import Mapping
from datetime import date, datetime
from typing import Any

import asyncpg

from .connection import get_pool

RETENTION_DAYS = 90
_COLUMNS = ("source", "source_ref", "manager_id", "channel_id", "chat", "chat_kind", "external_id", "outcome",
            "dedupe_key")


async def insert(row: Mapping[str, Any]) -> dict[str, Any]:
    """Refresh the receipt on retries, preserving the first decision and its source."""
    result = await get_pool().fetchrow(
        f"INSERT INTO proactive_deliveries ({', '.join(_COLUMNS)}, sent_at) "
        "VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,COALESCE($10::timestamptz, "
        "CASE WHEN $8::text IS NOT NULL THEN now() END)) "
        "ON CONFLICT (dedupe_key) WHERE dedupe_key IS NOT NULL DO UPDATE "
        "SET outcome=EXCLUDED.outcome, external_id=EXCLUDED.external_id, sent_at=EXCLUDED.sent_at RETURNING *",
        *(row.get(name) for name in _COLUMNS), row.get("sent_at"))
    return dict(result)


async def mark_replied(channel_id: str, chat: str, quoted_external_id: str | None, now: datetime) -> None:
    await get_pool().execute(
        "UPDATE proactive_deliveries SET replied_at=$4, "
        "reply_match=CASE WHEN external_id=$3 THEN 'quoted' ELSE 'next_message' END "
        "WHERE channel_id=$1 AND chat=$2 AND replied_at IS NULL AND sent_at IS NOT NULL "
        "AND sent_at >= $4::timestamptz - interval '30 minutes' AND sent_at <= $4",
        channel_id, chat, quoted_external_id, now)


async def summary(days: int) -> dict[str, Any]:
    from ..agents.cron import local_zone_name

    db = get_pool()
    per_day: dict[str, dict[str, int]] = {}
    rows = await db.fetch(
        "SELECT (sent_at AT TIME ZONE $2)::date AS date, source, count(*) AS n FROM proactive_deliveries "
        "WHERE sent_at IS NOT NULL AND sent_at >= now() - make_interval(days => $1) "
        "GROUP BY 1,2 ORDER BY 1,2", days, local_zone_name())
    for row in rows:
        per_day.setdefault(row["date"].isoformat(), {})[row["source"]] = row["n"]
    rows = await db.fetch(
        "SELECT source, count(*) AS n, count(replied_at) AS replied FROM proactive_deliveries "
        "WHERE sent_at IS NOT NULL AND outcome='sent' "
        "AND sent_at >= now() - make_interval(days => $1) AND sent_at <= now() - interval '30 minutes' "
        "GROUP BY source", days)
    count = sum(row["n"] for row in rows)
    return {"per_day": [{"date": day, "by_source": counts} for day, counts in per_day.items()],
            "reply_rate": {"all": sum(row["replied"] for row in rows) / count if count else None,
                           "by_source": {row["source"]: row["replied"] / row["n"] for row in rows}}}


async def purge(before: datetime) -> int:
    result = await get_pool().execute(
        "DELETE FROM proactive_deliveries WHERE id IN "
        "(SELECT id FROM proactive_deliveries WHERE COALESCE(sent_at, decided_at) < $1 LIMIT 5000)", before)
    return int(result.split()[-1])


# What a decision writes; the receipt (outcome, sent_at, external_id) comes after the send.
_DECISION_COLUMNS = ("dedupe_key", "source", "source_ref", "manager_id", "urgency", "channel_id", "chat", "chat_kind",
                     "task_id", "step_id", "schedule_id", "decision", "reason", "counted", "local_date",
                     "digest_date", "decided_at")
_DECIDED = ("urgency", "task_id", "step_id", "schedule_id", "decision", "reason", "counted", "local_date",
            "digest_date", "decided_at")


async def lock_chat(conn: asyncpg.Connection, channel_id: str, chat: str) -> None:
    """Serialize the decisions on one chat until this transaction ends, so two send points cannot both take the
    chat's last message of the day."""
    await conn.execute("SELECT pg_advisory_xact_lock(hashtextextended($1, 0))", f"proactive:{channel_id}:{chat}")


async def get(conn: asyncpg.Connection | asyncpg.Pool, dedupe_key: str) -> dict[str, Any] | None:
    row = await conn.fetchrow("SELECT * FROM proactive_deliveries WHERE dedupe_key = $1", dedupe_key)
    return None if row is None else dict(row)


async def counted_on(conn: asyncpg.Connection, channel_id: str, chat: str, local_date: date) -> int:
    """How many counted messages the chat had on ``local_date``."""
    return await conn.fetchval("SELECT count(*) FROM proactive_deliveries WHERE channel_id = $1 AND chat = $2 "
                               "AND local_date = $3 AND counted", channel_id, chat, local_date)


async def insert_decision(conn: asyncpg.Connection, row: Mapping[str, Any]) -> dict[str, Any]:
    """Write a decision: a new row, or the decision of a row the metrics recorded without one. A row decided on
    already (another decision on the same message got in first) is returned as it is."""
    marks = ", ".join(f"${n}" for n in range(1, len(_DECISION_COLUMNS) + 1))
    added = await conn.fetchrow(
        f"INSERT INTO proactive_deliveries ({', '.join(_DECISION_COLUMNS)}) VALUES ({marks}) "
        "ON CONFLICT (dedupe_key) WHERE dedupe_key IS NOT NULL DO UPDATE SET "
        + ", ".join(f"{name} = EXCLUDED.{name}" for name in _DECIDED)
        + " WHERE proactive_deliveries.decision IS NULL RETURNING *",
        *(row[name] for name in _DECISION_COLUMNS))
    return dict(added) if added is not None else await get(conn, row["dedupe_key"])


async def record_receipt(dedupe_key: str, outcome: str, external_id: str | None = None) -> None:
    """Note what the channel did with a decided message. A receipt already recorded stands, except a ``rejected``
    one: a later try of the same message replaces its outcome, sent time and external id. One the metrics wrote as
    sent or unknown stands."""
    await get_pool().execute(
        "UPDATE proactive_deliveries SET outcome = $2, sent_at = now(), "
        "external_id = CASE WHEN outcome IS NULL THEN COALESCE(external_id, $3) ELSE $3 END "
        "WHERE dedupe_key = $1 AND decision = 'send' AND (outcome IS NULL OR outcome = 'rejected')",
        dedupe_key, outcome, external_id)
