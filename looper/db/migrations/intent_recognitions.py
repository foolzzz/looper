"""Operator-confirmed, bounded, one-off recognition runs, independent of subscription cursors."""

from . import _widen_check

name = "intent_recognitions"
order = 1810
pending_order = 1810
fresh_ddl = """
CREATE TABLE IF NOT EXISTS intent_recognitions (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    source_id TEXT NOT NULL,
    chat_id TEXT NOT NULL,
    schedule_id TEXT REFERENCES schedules(id) ON DELETE SET NULL,
    range_from TIMESTAMPTZ NOT NULL,
    range_to TIMESTAMPTZ NOT NULL,
    requested_via TEXT NOT NULL CHECK (requested_via IN ('manager', 'dashboard')),
    reply_channel TEXT,
    reply_chat TEXT,
    state TEXT NOT NULL DEFAULT 'running' CHECK (state IN ('running', 'done', 'failed')),
    events_in_range INTEGER NOT NULL CHECK (events_in_range >= 0),
    events_read INTEGER NOT NULL DEFAULT 0 CHECK (events_read >= 0),
    truncated TEXT CHECK (truncated IN ('messages', 'budget')),
    cost_usd DOUBLE PRECISION NOT NULL DEFAULT 0,
    counts JSONB,
    error TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at TIMESTAMPTZ,
    FOREIGN KEY (source_id, chat_id) REFERENCES source_chats(source_id, chat_id) ON DELETE CASCADE,
    CHECK (range_from < range_to),
    CHECK ((state = 'running') = (finished_at IS NULL)),
    CHECK ((reply_channel IS NULL) = (reply_chat IS NULL))
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_intent_recognitions_running ON intent_recognitions (source_id)
    WHERE state = 'running';
ALTER TABLE intents ADD COLUMN IF NOT EXISTS recognition_id BIGINT REFERENCES intent_recognitions(id) ON DELETE CASCADE;
"""
fresh_ddl += _widen_check.ddl("intents", "via", "intents_via_check", ("on_request",))
fresh_ddl += """
ALTER TABLE intents VALIDATE CONSTRAINT intents_via_check;
ALTER TABLE intents DROP CONSTRAINT IF EXISTS intents_on_request;
ALTER TABLE intents ADD CONSTRAINT intents_on_request CHECK ((via = 'on_request') = (recognition_id IS NOT NULL));
"""


async def pending(conn):
    # A repaired earlier migration may recreate via and remove its dependent checks.
    return not await conn.fetchval(
        "SELECT to_regclass('intent_recognitions') IS NOT NULL "
        "AND EXISTS (SELECT 1 FROM pg_attribute WHERE attrelid=to_regclass('intents') "
        "AND attname='recognition_id' AND attnum>0 AND NOT attisdropped) "
        "AND EXISTS (SELECT 1 FROM pg_constraint WHERE conrelid=to_regclass('intents') "
        "AND conname='intents_on_request') "
        "AND EXISTS (SELECT 1 FROM pg_constraint WHERE conrelid=to_regclass('intents') "
        "AND conname='intents_via_check' AND strpos(pg_get_constraintdef(oid), $1)>0)", "'on_request'")


async def apply(conn, *, sql, state):
    if await pending(conn):
        await conn.execute(fresh_ddl)
        await conn.execute("INSERT INTO schema_migrations (name) VALUES ($1) ON CONFLICT DO NOTHING", name)
