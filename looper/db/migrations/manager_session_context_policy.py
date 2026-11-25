"""Identify routed histories recorded before the input and tool allowlist."""

name = "manager_session_context_policy"
order = 2000
pending_order = 2000
fresh_ddl = (
    "ALTER TABLE manager_sessions ADD COLUMN IF NOT EXISTS "
    "context_policy_version INTEGER NOT NULL DEFAULT 0;"
)


async def pending(conn):
    return not await conn.fetchval(
        "SELECT EXISTS (SELECT 1 FROM pg_attribute WHERE attrelid = to_regclass('manager_sessions') "
        "AND attname = 'context_policy_version' AND NOT attisdropped)")


async def apply(conn, *, sql, state):
    if await pending(conn):
        await conn.execute(fresh_ddl)
        await conn.execute("INSERT INTO schema_migrations (name) VALUES ($1) ON CONFLICT DO NOTHING", name)
