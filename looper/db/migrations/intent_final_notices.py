"""Persist bounded final-notice retries without rewriting existing intents or posts."""

name = "intent_final_notices"
order = 700
pending_order = 700
fresh_ddl = """
CREATE TABLE IF NOT EXISTS intent_final_notices (
    push_id BIGINT PRIMARY KEY REFERENCES intent_posts(id) ON DELETE CASCADE,
    handled_at TIMESTAMPTZ NOT NULL,
    final_post_id BIGINT REFERENCES intent_posts(id) ON DELETE SET NULL,
    state TEXT NOT NULL DEFAULT 'pending' CHECK (state IN ('pending', 'sent', 'failed')),
    attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    next_attempt_at TIMESTAMPTZ NOT NULL,
    failure_reason TEXT CHECK (failure_reason IN ('final_notice_retry_exhausted')),
    updated_at TIMESTAMPTZ NOT NULL,
    CHECK ((state = 'failed') = (failure_reason IS NOT NULL))
);
CREATE INDEX IF NOT EXISTS idx_intent_final_notices_failed ON intent_final_notices(updated_at) WHERE state='failed';
"""


async def pending(conn):
    if await conn.fetchval("SELECT EXISTS (SELECT 1 FROM schema_migrations WHERE name = $1)", name):
        return False
    return not await conn.fetchval("SELECT to_regclass('intent_final_notices') IS NOT NULL")


async def apply(conn, *, sql, state):
    if await pending(conn):
        await conn.execute(fresh_ddl)
        await conn.execute("INSERT INTO schema_migrations (name) VALUES ($1)", name)
