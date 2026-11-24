"""Separate equivalent-spend daily caps for schedules using enforced cost admission."""
from __future__ import annotations

import asyncpg

name = "schedule_equivalent_budget"
order = 1600
pending_order = 1600
fresh_ddl = """
ALTER TABLE schedules ADD COLUMN IF NOT EXISTS daily_budget_equivalent_usd DOUBLE PRECISION
  CHECK (daily_budget_equivalent_usd > 0);
"""


async def pending(conn: asyncpg.Connection) -> bool:
    return not await conn.fetchval(
        "SELECT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema = current_schema() "
        "AND table_name = 'schedules' AND column_name = 'daily_budget_equivalent_usd')")


async def apply(conn: asyncpg.Connection, *, sql: str, state: str) -> None:
    if await pending(conn):
        await conn.execute(fresh_ddl)
        await conn.execute("INSERT INTO schema_migrations (name) VALUES ($1) ON CONFLICT DO NOTHING", name)
