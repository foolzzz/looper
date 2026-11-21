"""Observational delivery metrics; callers schedule writes after sending."""
from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Any

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
