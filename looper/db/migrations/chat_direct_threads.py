"""Session search's chat direct threads on fresh and upgraded databases."""

name = "chat_direct_threads"
order = 660
pending_order = 660
fresh_ddl = """
CREATE TABLE IF NOT EXISTS chat_direct_threads (
    session_id  TEXT PRIMARY KEY,
    channel_id  TEXT NOT NULL REFERENCES channel_configs(channel_id) ON DELETE CASCADE,
    sender_id   TEXT NOT NULL CHECK (btrim(sender_id) <> ''),
    recorded_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
"""


async def pending(conn):
    if await conn.fetchval("SELECT EXISTS (SELECT 1 FROM schema_migrations WHERE name = $1)", name):
        return False
    return not await conn.fetchval("SELECT to_regclass('chat_direct_threads') IS NOT NULL")


async def apply(conn, *, sql, state):
    if await pending(conn):
        await conn.execute(fresh_ddl)
        await conn.execute("INSERT INTO schema_migrations (name) VALUES ($1)", name)
