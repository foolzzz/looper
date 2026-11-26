"""The runtime_launcher migration, in its released execution order."""

name = "runtime_launcher"
order = 70
pending_order = 70


async def pending(conn):
    from .. import connection as legacy

    return await legacy._runtime_launcher_pending(conn)


async def apply(conn, *, sql, state) -> bool:
    did_work = False
    from .. import connection as legacy

    if await legacy._runtime_launcher_pending(conn):
        did_work = True
        await conn.execute(
            "ALTER TABLE agent_runtimes ADD COLUMN IF NOT EXISTS launcher "
            "TEXT NOT NULL DEFAULT 'native' CHECK (launcher IN ('native', 'ttadk'))"
        )
        await conn.execute(
            "INSERT INTO schema_migrations (name) VALUES ($1)", legacy._RUNTIME_LAUNCHER
        )
    return did_work
