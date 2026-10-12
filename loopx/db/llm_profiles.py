"""LLM profile CRUD — provider + model connection config."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

import asyncpg

from .connection import get_pool

_UPDATABLE_FIELDS = (
    "name",
    "display_name",
    "provider_kind",
    "model",
    "base_url",
    "api_key_env",
    "reasoning_effort",
    "max_tokens",
    "context_window_k",
    "rate_per_minute",
    "streaming",
    "is_default",
    "priority",
    "description",
    "metadata",
)


def _row_to_dict(row: asyncpg.Record) -> dict[str, Any]:
    d = dict(row)
    for key in ("created_at", "updated_at"):
        if key in d and d[key] is not None:
            d[key] = d[key].isoformat()
    if "metadata" in d and d["metadata"] is not None:
        d["metadata"] = json.loads(d["metadata"])
    return d


async def list_profiles(*, pool: asyncpg.Pool | None = None) -> list[dict[str, Any]]:
    pool = pool or get_pool()
    rows = await pool.fetch("SELECT * FROM llm_profiles ORDER BY priority DESC, name")
    return [_row_to_dict(r) for r in rows]


async def get_profile(profile_id: int, *, pool: asyncpg.Pool | None = None) -> dict[str, Any] | None:
    pool = pool or get_pool()
    row = await pool.fetchrow("SELECT * FROM llm_profiles WHERE id = $1", profile_id)
    if row is None:
        return None
    return _row_to_dict(row)


async def create_profile(
    fields: dict[str, Any],
    *,
    pool: asyncpg.Pool | None = None,
) -> dict[str, Any]:
    pool = pool or get_pool()
    name = fields.get("name", "").strip()
    if not name:
        raise ValueError("name is required")

    meta_json = json.dumps(fields["metadata"]) if fields.get("metadata") else None

    row = await pool.fetchrow(
        """
        INSERT INTO llm_profiles
            (name, display_name, provider_kind, model, base_url, api_key_env,
             reasoning_effort, max_tokens, context_window_k, rate_per_minute,
             streaming, is_default, priority, description, metadata)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15)
        RETURNING id
        """,
        name,
        fields.get("display_name", ""),
        fields.get("provider_kind", "anthropic"),
        fields.get("model", ""),
        fields.get("base_url"),
        fields.get("api_key_env"),
        fields.get("reasoning_effort"),
        fields.get("max_tokens"),
        fields.get("context_window_k"),
        fields.get("rate_per_minute"),
        fields.get("streaming", True),
        fields.get("is_default", False),
        fields.get("priority", 0),
        fields.get("description", ""),
        meta_json,
    )
    return (await get_profile(row["id"], pool=pool))  # type: ignore[return-value]


async def update_profile(
    profile_id: int,
    fields: dict[str, Any],
    *,
    pool: asyncpg.Pool | None = None,
) -> dict[str, Any] | None:
    """Partial-patch update."""
    pool = pool or get_pool()

    updates = {}
    for key in _UPDATABLE_FIELDS:
        if key in fields:
            updates[key] = fields[key]

    if not updates:
        return await get_profile(profile_id, pool=pool)

    if "metadata" in updates and updates["metadata"] is not None:
        updates["metadata"] = json.dumps(updates["metadata"])

    set_clauses = []
    values: list[Any] = []
    for i, (key, value) in enumerate(updates.items(), start=2):
        set_clauses.append(f"{key} = ${i}")
        values.append(value)

    set_clauses.append(f"updated_at = ${len(values) + 2}")
    values.append(datetime.now(timezone.utc))

    sql = f"UPDATE llm_profiles SET {', '.join(set_clauses)} WHERE id = $1"
    await pool.execute(sql, profile_id, *values)
    return await get_profile(profile_id, pool=pool)


async def delete_profile(
    profile_id: int,
    *,
    pool: asyncpg.Pool | None = None,
) -> bool:
    pool = pool or get_pool()
    result = await pool.execute("DELETE FROM llm_profiles WHERE id = $1", profile_id)
    return result == "DELETE 1"


async def seed_defaults(
    defaults: list[dict[str, Any]],
    *,
    pool: asyncpg.Pool | None = None,
) -> None:
    """Insert default profiles that don't exist yet (by name)."""
    pool = pool or get_pool()
    for p in defaults:
        name = p["name"]
        await pool.execute(
            """
            INSERT INTO llm_profiles
                (name, display_name, provider_kind, model, description)
            VALUES ($1, $2, $3, $4, $5)
            ON CONFLICT (name) DO NOTHING
            """,
            name,
            p.get("display_name", ""),
            p.get("provider_kind", "anthropic"),
            p.get("model", ""),
            p.get("description", ""),
        )
