"""Channel configuration CRUD — DB-driven config for Lark/Telegram channels."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Literal

import asyncpg

from .connection import get_pool

_UPDATABLE_FIELDS = ("display_name", "enabled", "config")

BUILTIN_CHANNEL_IDS = ("channel-telegram", "channel-lark", "channel-lark_bot")


def _row_to_dict(row: asyncpg.Record) -> dict[str, Any]:
    d = dict(row)
    for key in ("created_at", "updated_at"):
        if key in d and d[key] is not None:
            d[key] = d[key].isoformat()
    if "config" in d and d["config"] is not None:
        d["config"] = json.loads(d["config"])
    return d


_SELECT_WITH_SECRET = (
    "SELECT c.*, m.name AS manager_name, s.channel_id IS NOT NULL AS has_bot_token "
    "FROM channel_configs c LEFT JOIN channel_secrets s ON s.channel_id = c.channel_id "
    "LEFT JOIN managers m ON m.id = c.manager_id"
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


async def create_bot(
    channel_type: str,
    manager_id: str,
    *,
    display_name: str,
    enabled: bool,
    config: dict[str, Any],
    secret: str | None,
    conn: asyncpg.Connection,
) -> dict[str, Any]:
    """Add a bot of ``channel_type`` for Manager ``manager_id`` on ``conn`` (the caller owns the transaction and the
    advisory lock). ``LookupError`` for an unknown or deleted Manager; ``FOR KEY SHARE`` keeps it from being
    deleted meanwhile."""
    ok = await conn.fetchval(
        "SELECT deleted_at IS NULL FROM managers WHERE id = $1 FOR KEY SHARE", manager_id
    )
    if not ok:
        raise LookupError(f"Manager {manager_id} not found")
    n = await conn.fetchval("SELECT nextval('channel_bot_seq')")
    channel_id = f"channel-{channel_type}-{n}"
    await conn.execute(
        "INSERT INTO channel_configs (channel_id, channel_type, display_name, enabled, config, manager_id) "
        "VALUES ($1, $2, $3, $4, $5, $6)",
        channel_id, channel_type, display_name, enabled, json.dumps(config), manager_id,
    )
    if secret:
        await set_bot_token(channel_id, secret, pool=conn)
    return await get_config(channel_id, pool=conn)  # type: ignore[return-value]


async def delete_bot(
    channel_id: str, *, pool: asyncpg.Pool | None = None
) -> Literal["deleted", "missing", "builtin"]:
    """Remove an added bot with its token and refused senders; its chat threads and messages stay."""
    if channel_id in BUILTIN_CHANNEL_IDS:
        return "builtin"
    pool = pool or get_pool()
    async with pool.acquire() as conn, conn.transaction():
        gone = await conn.fetchval(
            "DELETE FROM channel_configs WHERE channel_id = $1 RETURNING channel_id", channel_id
        )
        if gone is None:
            return "missing"
        await conn.execute(
            "DELETE FROM channel_rejected_senders WHERE channel_id = $1", channel_id
        )
    return "deleted"


async def bot_keys(channel_type: str, *, conn: asyncpg.Connection) -> list[dict[str, Any]]:
    """Every bot of ``channel_type`` with its config and stored secret, for the duplicate check (server code only)."""
    rows = await conn.fetch(
        "SELECT c.channel_id, c.config, s.bot_token AS secret FROM channel_configs c "
        "LEFT JOIN channel_secrets s ON s.channel_id = c.channel_id WHERE c.channel_type = $1 "
        "ORDER BY c.channel_id", channel_type,
    )
    out = []
    for r in rows:
        d = dict(r)
        if d["config"] is not None:
            d["config"] = json.loads(d["config"])
        out.append(d)
    return out


async def list_configs(*, pool: asyncpg.Pool | None = None) -> list[dict[str, Any]]:
    pool = pool or get_pool()
    rows = await pool.fetch(f"{_SELECT_WITH_SECRET} ORDER BY (c.channel_id ~ '-[0-9]+$'), c.channel_id")
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


REJECTED_KEPT = 20


async def record_rejected(channel_id: str, sender_id: str, *, chat_id: str | None = None,
                          pool: asyncpg.Pool | None = None) -> None:
    """Record that the channel's allowlist refused a message from ``sender_id`` (never what it said), in group chat
    ``chat_id`` (None for a direct message); the channel keeps its ``REJECTED_KEPT`` newest senders."""
    pool = pool or get_pool()
    async with pool.acquire() as conn, conn.transaction():
        await conn.execute(
            """
            INSERT INTO channel_rejected_senders (channel_id, sender_id, chat_id) VALUES ($1, $2, $3)
            ON CONFLICT (channel_id, sender_id)
            DO UPDATE SET count = channel_rejected_senders.count + 1, last_at = now(), chat_id = EXCLUDED.chat_id
            """,
            channel_id, sender_id, chat_id,
        )
        await conn.execute(
            """
            DELETE FROM channel_rejected_senders WHERE channel_id = $1 AND sender_id NOT IN (
                SELECT sender_id FROM channel_rejected_senders WHERE channel_id = $1
                ORDER BY last_at DESC LIMIT $2)
            """,
            channel_id, REJECTED_KEPT,
        )


async def rejected_senders(channel_id: str, *, pool: asyncpg.Pool | None = None) -> list[dict[str, Any]]:
    """The channel's latest refused senders, newest first."""
    pool = pool or get_pool()
    rows = await pool.fetch(
        "SELECT sender_id, count, last_at, chat_id FROM channel_rejected_senders WHERE channel_id = $1 "
        "ORDER BY last_at DESC LIMIT $2", channel_id, REJECTED_KEPT,
    )
    return [dict(r) for r in rows]


async def drop_config_keys(channel_type: str, keys: list[str], *, pool: asyncpg.Pool | None = None) -> list[str]:
    """Remove ``keys`` from the configs of ``channel_type``'s channels; the ids of the channels that held one."""
    pool = pool or get_pool()
    rows = await pool.fetch(
        "UPDATE channel_configs SET config = config - $2::text[], updated_at = now() "
        "WHERE channel_type = $1 AND config ?| $2::text[] RETURNING channel_id",
        channel_type, keys,
    )
    return [r["channel_id"] for r in rows]
