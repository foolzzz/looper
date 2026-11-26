"""The operator profile's cached compiled block."""

import asyncpg

name = "profile_blocks"
order = 710
pending_order = 710
fresh_ddl = """
CREATE TABLE IF NOT EXISTS profile_blocks (
    id           BOOLEAN PRIMARY KEY DEFAULT TRUE CHECK (id),
    version      BIGINT NOT NULL CHECK (version >= 1),
    text         TEXT NOT NULL CHECK (char_length(text) <= 1500),
    body         JSONB NOT NULL CHECK (jsonb_typeof(body) = 'object'),
    compiled_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
"""


async def pending(conn: asyncpg.Connection) -> bool:
    if await conn.fetchval("SELECT EXISTS (SELECT 1 FROM schema_migrations WHERE name = $1)", name):
        return False
    return not await conn.fetchval("SELECT to_regclass('profile_blocks') IS NOT NULL")


async def apply(conn, *, sql, state) -> bool:
    did_work = False
    if await pending(conn):
        did_work = True
        await conn.execute(fresh_ddl)
        await conn.execute("INSERT INTO schema_migrations (name) VALUES ($1)", name)
    return did_work
