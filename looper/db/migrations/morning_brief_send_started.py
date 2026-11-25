"""Distinguish a morning brief claim from a send that may have reached the channel."""

name = "morning_brief_send_started"
order = 1930
pending_order = 1930
fresh_ddl = "ALTER TABLE morning_briefs ADD COLUMN IF NOT EXISTS send_started_at TIMESTAMPTZ;"


async def pending(conn):
    if not await conn.fetchval("SELECT to_regclass('morning_briefs') IS NOT NULL"):
        return not await conn.fetchval(
            "SELECT EXISTS (SELECT 1 FROM schema_migrations WHERE name = $1)", name)
    return not await conn.fetchval(
        "SELECT EXISTS (SELECT 1 FROM pg_attribute WHERE attrelid = 'morning_briefs'::regclass "
        "AND attname = 'send_started_at' AND attnum > 0 AND NOT attisdropped)")


async def apply(conn, *, sql, state):
    if await pending(conn):
        await conn.execute(fresh_ddl)
        # Old claims cannot prove that no send started; retain their no-resend recovery policy.
        await conn.execute("UPDATE morning_briefs SET send_started_at = updated_at WHERE status = 'sending'")
        await conn.execute("INSERT INTO schema_migrations (name) VALUES ($1) ON CONFLICT DO NOTHING", name)
