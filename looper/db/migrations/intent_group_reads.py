"""Opt-in, per-chat followed-group intent reads and their daily allowances."""

from . import _widen_check

name = "intent_group_reads"
order = 1800
pending_order = 1800
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
DO $migration$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint c
        JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = ANY (c.conkey)
        WHERE c.conrelid = to_regclass('intents') AND c.contype = 'c' AND a.attname = 'via'
              AND a.attnum > 0 AND NOT a.attisdropped AND cardinality(c.conkey) = 1
    ) THEN
        ALTER TABLE intents ADD CONSTRAINT intents_via_check CHECK (via IN ('subscription')) NOT VALID;
    END IF;
END
$migration$;
"""
fresh_ddl += _widen_check.ddl("intents", "via", "intents_via_check", ("followed_group",))
fresh_ddl += "ALTER TABLE intents VALIDATE CONSTRAINT intents_via_check;\n"


async def pending(conn):
    # The marker can outlive tables or columns recreated by another upgrade.
    return not await conn.fetchval(
        "SELECT to_regclass('intent_group_reads') IS NOT NULL "
        "AND to_regclass('intent_group_read_days') IS NOT NULL "
        "AND EXISTS (SELECT 1 FROM pg_attribute WHERE attrelid = to_regclass('intents') "
        "AND attname = 'via' AND attnum > 0 AND NOT attisdropped) "
        "AND EXISTS (SELECT 1 FROM pg_constraint c "
        "JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = ANY (c.conkey) "
        "WHERE c.conrelid = to_regclass('intents') AND c.contype = 'c' AND a.attname = 'via' "
        "AND a.attnum > 0 AND NOT a.attisdropped AND cardinality(c.conkey) = 1 "
        "AND strpos(pg_get_constraintdef(c.oid), $1) > 0)", "'followed_group'")


async def apply(conn, *, sql, state) -> bool:
    did_work = False
    if await pending(conn):
        did_work = True
        await conn.execute(fresh_ddl)
        await conn.execute("INSERT INTO schema_migrations (name) VALUES ($1) ON CONFLICT DO NOTHING", name)
    return did_work
