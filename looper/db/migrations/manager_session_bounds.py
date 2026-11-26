"""Nullable measurements for Manager session-budget maintenance."""

from __future__ import annotations

import asyncpg

name = "manager_session_bounds"
order = 660
pending_order = 580
fresh_ddl = """
ALTER TABLE agent_sessions
  ADD COLUMN IF NOT EXISTS context_tokens INTEGER,
  ADD COLUMN IF NOT EXISTS last_turn_cost_usd DOUBLE PRECISION;
"""


async def pending(conn: asyncpg.Connection) -> bool:
    if await conn.fetchval("SELECT EXISTS (SELECT 1 FROM schema_migrations WHERE name = $1)", name):
        return False
    return await conn.fetchval(
        "SELECT count(*) < 2 FROM pg_attribute WHERE attrelid = to_regclass('agent_sessions') "
        "AND attname IN ('context_tokens', 'last_turn_cost_usd') AND attnum > 0 AND NOT attisdropped")


async def apply(conn: asyncpg.Connection, *, sql: str, state: str) -> bool:
    did_work = False
    if await pending(conn):
        did_work = True
        await conn.execute(fresh_ddl)
        await conn.execute("INSERT INTO schema_migrations (name) VALUES ($1)", name)
    return did_work
