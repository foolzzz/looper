"""Opt-in group sessions on scenario Manager definitions."""

name = "scenario_session_routing"
order = 2030
pending_order = 2030
fresh_ddl = """
ALTER TABLE managers ADD COLUMN IF NOT EXISTS session_routing TEXT NOT NULL DEFAULT 'off'
  CHECK (session_routing IN ('off', 'per_chat'));
"""


async def pending(conn):
    return not await conn.fetchval(
        "SELECT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema = current_schema() "
        "AND table_name = 'managers' AND column_name = 'session_routing')")


async def apply(conn, *, sql, state) -> bool:
    did_work = False
    if await pending(conn):
        did_work = True
        await conn.execute(fresh_ddl)
        await conn.execute("INSERT INTO schema_migrations (name) VALUES ($1) ON CONFLICT DO NOTHING", name)
    return did_work
