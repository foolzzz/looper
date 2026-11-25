"""Bounded, short-lived text of proactive sends, separate from delivery policy."""
import asyncpg

name = "proactive_bodies"
order = 2110
pending_order = 2110
fresh_ddl = """
ALTER TABLE proactive_deliveries ADD COLUMN IF NOT EXISTS body TEXT;
ALTER TABLE proactive_deliveries ADD COLUMN IF NOT EXISTS body_truncated BOOLEAN NOT NULL DEFAULT false;
ALTER TABLE proactive_deliveries ADD COLUMN IF NOT EXISTS body_stored_at TIMESTAMPTZ;
"""


async def pending(conn: asyncpg.Connection) -> bool:
    from .. import connection

    return await connection._column_pending(conn, name, "proactive_deliveries", "body")


async def apply(conn: asyncpg.Connection, *, sql: str, state: str) -> None:
    if await pending(conn):
        await conn.execute(fresh_ddl)
        await conn.execute("INSERT INTO schema_migrations (name) VALUES ($1)", name)
