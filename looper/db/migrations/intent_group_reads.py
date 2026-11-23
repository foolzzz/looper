"""Opt-in, per-chat followed-group intent reads and their daily allowances."""

name = "intent_group_reads"
order = 1500
pending_order = 1500
fresh_ddl = """
CREATE TABLE IF NOT EXISTS intent_group_reads (
    source_id TEXT NOT NULL,
    chat_id TEXT NOT NULL,
    added_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    read_from TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (source_id, chat_id),
    FOREIGN KEY (source_id, chat_id) REFERENCES source_chats(source_id, chat_id) ON DELETE CASCADE,
    CHECK (read_from >= added_at)
);
CREATE TABLE IF NOT EXISTS intent_group_read_days (
    source_id TEXT NOT NULL,
    chat_id TEXT NOT NULL,
    day DATE NOT NULL,
    read INTEGER NOT NULL DEFAULT 0 CHECK (read >= 0),
    skipped INTEGER NOT NULL DEFAULT 0 CHECK (skipped >= 0),
    PRIMARY KEY (source_id, chat_id, day),
    FOREIGN KEY (source_id, chat_id) REFERENCES source_chats(source_id, chat_id) ON DELETE CASCADE
);
ALTER TABLE intents ADD COLUMN IF NOT EXISTS via TEXT NOT NULL DEFAULT 'subscription';
ALTER TABLE intents DROP CONSTRAINT IF EXISTS intents_via_check;
ALTER TABLE intents ADD CONSTRAINT intents_via_check CHECK (via IN ('subscription', 'followed_group'));
"""


async def pending(conn):
    if await conn.fetchval("SELECT EXISTS (SELECT 1 FROM schema_migrations WHERE name = $1)", name):
        return False
    return not await conn.fetchval("SELECT to_regclass('intent_group_reads') IS NOT NULL")


async def apply(conn, *, sql, state):
    if await pending(conn):
        await conn.execute(fresh_ddl)
        await conn.execute("INSERT INTO schema_migrations (name) VALUES ($1)", name)
