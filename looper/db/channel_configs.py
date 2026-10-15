"""Channel configuration CRUD — DB-driven config for Lark/Telegram channels."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

import asyncpg

from .connection import get_pool

_UPDATABLE_FIELDS = ("display_name", "enabled", "config")


def _row_to_dict(row: asyncpg.Record) -> dict[str, Any]:
    d = dict(row)
    for key in ("created_at", "updated_at"):
        if key in d and d[key] is not None:
            d[key] = d[key].isoformat()
    if "config" in d and d["config"] is not None:
        d["config"] = json.loads(d["config"])
    return d


_SELECT_WITH_SECRET = (
    "SELECT c.*, s.channel_id IS NOT NULL AS has_bot_token "
    "FROM channel_configs c LEFT JOIN channel_secrets s ON s.channel_id = c.channel_id"
)


async def get_config(
    channel_id: str, *, pool: asyncpg.Pool | None = None
) -> dict[str, Any] | None:
    pool = pool or get_pool()
    row = await pool.fetchrow(
        f"{_SELECT_WITH_SECRET} WHERE c.channel_id = $1", channel_id
    )
    if row is None:
        return None
    return _row_to_dict(row)


async def get_bot_token(
    channel_id: str, *, pool: asyncpg.Pool | None = None
) -> str | None:
    """The channel's bot token, for the launch that builds its adapter; None without one."""
    pool = pool or get_pool()
    return await pool.fetchval(
        "SELECT bot_token FROM channel_secrets WHERE channel_id = $1", channel_id
    )


async def set_bot_token(
    channel_id: str, token: str | None, *, pool: asyncpg.Pool | None = None
) -> None:
    """Store (or, with None, remove) the channel's bot token. It never goes through ``config``."""
    pool = pool or get_pool()
    if token is None:
        await pool.execute(
            "DELETE FROM channel_secrets WHERE channel_id = $1", channel_id
        )
    else:
        await pool.execute(
            "INSERT INTO channel_secrets (channel_id, bot_token) VALUES ($1, $2) "
            "ON CONFLICT (channel_id) DO UPDATE SET bot_token = EXCLUDED.bot_token",
            channel_id,
            token,
        )


async def set_config(
    channel_id: str,
    fields: dict[str, Any],
    *,
    pool: asyncpg.Pool | None = None,
) -> dict[str, Any] | None:
    pool = pool or get_pool()

    updates: dict[str, Any] = {}
    for key in _UPDATABLE_FIELDS:
        if key in fields:
            updates[key] = fields[key]

    if not updates:
        return await get_config(channel_id, pool=pool)

    if "config" in updates and updates["config"] is not None:
        updates["config"] = json.dumps(updates["config"])

    set_clauses = []
    values: list[Any] = []
    for i, (key, value) in enumerate(updates.items(), start=2):
        set_clauses.append(f"{key} = ${i}")
        values.append(value)

    set_clauses.append(f"updated_at = ${len(values) + 2}")
    values.append(datetime.now(timezone.utc))

    sql = f"UPDATE channel_configs SET {', '.join(set_clauses)} WHERE channel_id = $1"
    await pool.execute(sql, channel_id, *values)
    return await get_config(channel_id, pool=pool)


async def list_configs(*, pool: asyncpg.Pool | None = None) -> list[dict[str, Any]]:
    pool = pool or get_pool()
    rows = await pool.fetch(f"{_SELECT_WITH_SECRET} ORDER BY c.channel_id")
    return [_row_to_dict(r) for r in rows]


async def seed_defaults(
    defaults: list[dict[str, Any]],
    *,
    pool: asyncpg.Pool | None = None,
) -> None:
    pool = pool or get_pool()
    for cfg in defaults:
        channel_id = cfg["channel_id"]
        channel_type = cfg.get("channel_type", "")
        display_name = cfg.get("display_name", "")
        enabled = cfg.get("enabled", False)
        config = json.dumps(cfg.get("config", {}))

        await pool.execute(
            """
            INSERT INTO channel_configs
                (channel_id, channel_type, display_name, enabled, config)
            VALUES ($1, $2, $3, $4, $5)
            ON CONFLICT (channel_id) DO NOTHING
            """,
            channel_id,
            channel_type,
            display_name,
            enabled,
            config,
        )
