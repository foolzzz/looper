"""The ``system_settings`` table: one text value per key.

Code owns each key's meaning, type and validation (such as the catalog's
last import report, ``looper.agents.catalog``); a key without a row has its
built-in default. Agent settings live in ``agent_setting_layers``.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

import asyncpg

from .connection import get_pool


async def get_settings(keys: Sequence[str], *, pool: asyncpg.Pool | None = None) -> dict[str, str]:
    """The stored values of ``keys``; a key without a row is absent."""
    pool = pool or get_pool()
    rows = await pool.fetch("SELECT key, value FROM system_settings WHERE key = ANY($1::text[])", list(keys))
    return {row["key"]: row["value"] for row in rows}


async def put_settings(values: Mapping[str, str | None], *, pool: asyncpg.Pool | None = None) -> None:
    """Store each value, in one transaction; None removes the key's row (its built-in default applies)."""
    pool = pool or get_pool()
    async with pool.acquire() as conn, conn.transaction():
        for key, value in values.items():
            if value is None:
                await conn.execute("DELETE FROM system_settings WHERE key = $1", key)
            else:
                await conn.execute(
                    "INSERT INTO system_settings (key, value) VALUES ($1, $2) "
                    "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()",
                    key, value,
                )
