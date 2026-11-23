"""The exact triage events read and consumed by a morning brief."""

name = "morning_brief_triage"
order = 1410
pending_order = 1410
fresh_ddl = """
ALTER TABLE morning_briefs ADD COLUMN IF NOT EXISTS triage_ids BIGINT[] NOT NULL DEFAULT '{}';
CREATE TABLE IF NOT EXISTS morning_brief_triage (
    event_id    BIGINT PRIMARY KEY REFERENCES triage_events(id) ON DELETE CASCADE,
    local_date  DATE NOT NULL,
    channel_id  TEXT NOT NULL,
    chat        TEXT NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
"""


async def pending(conn):
    """Probe both objects even when an earlier upgrade already wrote the marker."""
    if not await conn.fetchval("SELECT to_regclass('morning_briefs') IS NOT NULL"):
        return not await conn.fetchval(
            "SELECT EXISTS (SELECT 1 FROM schema_migrations WHERE name = $1)", name)
    missing_column = not await conn.fetchval(
        "SELECT EXISTS (SELECT 1 FROM pg_attribute WHERE attrelid = 'morning_briefs'::regclass "
        "AND attname = 'triage_ids' AND attnum > 0 AND NOT attisdropped)")
    missing_table = not await conn.fetchval("SELECT to_regclass('morning_brief_triage') IS NOT NULL")
    return missing_column or missing_table


async def apply(conn, *, sql, state):
    if await pending(conn):
        await conn.execute(fresh_ddl)
        await conn.execute("INSERT INTO schema_migrations (name) VALUES ($1) ON CONFLICT DO NOTHING", name)
