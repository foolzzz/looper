"""Observed account windows and usage already present in run records; no admission decisions."""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from . import system_settings
from .connection import get_pool

_FIELDS = ("status", "utilization", "resets_at", "window_minutes", "source", "record_id", "observed_at")


async def upsert(provider_id: str, window_name: str, fields: Mapping[str, Any], *, pool=None) -> None:
    """Apply a newer observation atomically. Missing optional fields keep their value; explicit NULL clears it."""
    if set(fields) - set(_FIELDS) or not {"status", "source", "observed_at"} <= fields.keys():
        raise ValueError("a limit observation requires status, source and observed_at, with only window fields")
    pool = pool or get_pool()
    # Column names come only from this module, never the caller. Presence and value are separate parameters.
    values = [fields.get(name) for name in _FIELDS]
    updates = ", ".join(f"{name}=CASE WHEN ${index + 10} THEN EXCLUDED.{name} ELSE provider_limit_windows.{name} END"
                        for index, name in enumerate(_FIELDS))
    await pool.execute(
        f"INSERT INTO provider_limit_windows(provider_id,window_name,{','.join(_FIELDS)}) "
        f"VALUES($1,$2,{','.join('$'+str(i) for i in range(3,10))}) "
        f"ON CONFLICT(provider_id,window_name) DO UPDATE SET {updates} "
        "WHERE EXCLUDED.observed_at > provider_limit_windows.observed_at",
        provider_id, window_name, *values, *(name in fields for name in _FIELDS))


async def windows(provider_id: str | None = None, *, pool=None) -> list[dict[str, Any]]:
    pool = pool or get_pool()
    return [dict(row) for row in await pool.fetch(
        "SELECT * FROM provider_limit_windows WHERE ($1::text IS NULL OR provider_id=$1) "
        "ORDER BY provider_id,window_name", provider_id)]


async def usage(provider_id: str | None = None, *, pool=None) -> list[dict[str, Any]]:
    """Runs started during the last seven UTC calendar days. An absent failure classifier is unknown, not zero."""
    pool = pool or get_pool()
    mode = (await system_settings.get_settings(["engine.failure_typing"], pool=pool)).get("engine.failure_typing")
    classified = mode in ("record", "act") and await pool.fetchval(
        "SELECT EXISTS (SELECT 1 FROM pg_attribute WHERE attrelid='llm_records'::regclass "
        "AND attname='failure_class' AND NOT attisdropped)")
    quota = "count(*) FILTER (WHERE failure_class='quota')" if classified else "NULL::bigint"
    return [dict(row) for row in await pool.fetch(f"""
        SELECT metadata->'profile'->'provider'->>'id' AS provider_id,
            (created_at AT TIME ZONE 'UTC')::date AS day, count(*) AS runs,
            sum(input_tokens)::bigint AS input_tokens, sum(output_tokens)::bigint AS output_tokens,
            sum(cache_read_tokens)::bigint AS cache_read_input_tokens,
            sum(cache_creation_tokens)::bigint AS cache_creation_input_tokens, sum(cost_usd) AS cost_usd,
            {quota} AS quota_failures
        FROM llm_records r
        WHERE created_at >= ((now() AT TIME ZONE 'UTC')::date - 6) AT TIME ZONE 'UTC'
            AND created_at <= now()
            AND metadata->'profile'->'provider'->>'id' IS NOT NULL
            AND ($1::text IS NULL OR metadata->'profile'->'provider'->>'id'=$1)
        GROUP BY 1,2 ORDER BY 1,2
        """, provider_id)]
