"""Timestamp new terminal outbox transitions without retimestamping legacy outcomes."""

name = "channel_outbox_terminal_at"
order = 635  # after channel_outbox creates the table
pending_order = 224
fresh_ddl = "ALTER TABLE channel_outbox ADD COLUMN IF NOT EXISTS terminal_at TIMESTAMPTZ;"


async def pending(conn):
    if await conn.fetchval("SELECT EXISTS (SELECT 1 FROM schema_migrations WHERE name = $1)", name):
        return False
    return not await conn.fetchval(
        "SELECT EXISTS (SELECT 1 FROM pg_attribute WHERE attrelid=to_regclass('channel_outbox') "
        "AND attname='terminal_at' AND NOT attisdropped)")


async def apply(conn, *, sql, state) -> bool:
    did_work = False
    if await pending(conn):
        did_work = True
        await conn.execute(fresh_ddl)
        await conn.execute("INSERT INTO schema_migrations (name) VALUES ($1)", name)
    return did_work
