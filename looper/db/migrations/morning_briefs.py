"""Morning brief claims and the exact delivery rows each claim consumed."""

import asyncpg

name = "morning_briefs"
order = 730
pending_order = 730
fresh_ddl = """
CREATE TABLE IF NOT EXISTS morning_briefs (
    local_date  DATE NOT NULL,
    channel_id  TEXT NOT NULL DEFAULT '',
    chat        TEXT NOT NULL DEFAULT '',
    status      TEXT NOT NULL CHECK (status IN ('sending', 'sent', 'skipped', 'uncertain', 'failed')),
    attempts    INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    detail      TEXT CHECK (detail IS NULL OR char_length(detail) <= 500),
    consumed_until TIMESTAMPTZ,       -- check time when this brief recorded the rows it read; not a watermark
    read_ids    BIGINT[] NOT NULL DEFAULT '{}',  -- digest ids this claim's read returned; recovery consumes these
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (local_date, channel_id, chat)
);
ALTER TABLE morning_briefs ADD COLUMN IF NOT EXISTS consumed_until TIMESTAMPTZ;
ALTER TABLE morning_briefs ADD COLUMN IF NOT EXISTS read_ids BIGINT[] NOT NULL DEFAULT '{}';
-- Digest rows a brief actually read. The next brief skips these ids. An uncommitted insert is not here.
CREATE TABLE IF NOT EXISTS morning_brief_consumed (
    delivery_id BIGINT PRIMARY KEY REFERENCES proactive_deliveries(id) ON DELETE CASCADE
);
"""


async def pending(conn: asyncpg.Connection) -> bool:
    """The table, ``consumed_until`` or ``read_ids`` on a table created before that column, or the consumed-id
    table. A marker without the morning briefs table is left alone."""
    if not await conn.fetchval("SELECT to_regclass('morning_briefs') IS NOT NULL"):
        return not await conn.fetchval(
            "SELECT EXISTS (SELECT 1 FROM schema_migrations WHERE name = $1)", name)
    missing_column = not await conn.fetchval(
        "SELECT EXISTS (SELECT 1 FROM pg_attribute WHERE attrelid = 'morning_briefs'::regclass "
        "AND attname = 'consumed_until' AND attnum > 0 AND NOT attisdropped)")
    missing_reads = not await conn.fetchval(
        "SELECT EXISTS (SELECT 1 FROM pg_attribute WHERE attrelid = 'morning_briefs'::regclass "
        "AND attname = 'read_ids' AND attnum > 0 AND NOT attisdropped)")
    missing_ids = not await conn.fetchval("SELECT to_regclass('morning_brief_consumed') IS NOT NULL")
    return missing_column or missing_reads or missing_ids


async def apply(conn, *, sql, state) -> bool:
    did_work = False
    if await pending(conn):
        did_work = True
        await conn.execute(fresh_ddl)
        await conn.execute(
            "INSERT INTO schema_migrations (name) VALUES ($1) ON CONFLICT DO NOTHING", name)
    return did_work
