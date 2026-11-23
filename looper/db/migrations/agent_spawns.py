"""The agent_spawns migration, in its released execution order."""

name = "agent_spawns"
order = 390
pending_order = 350


async def pending(conn):
    from .. import connection as legacy

    return await legacy._agent_spawns_pending(conn)


async def apply(conn, *, sql, state):
    from .. import connection as legacy

    if await legacy._agent_spawns_pending(conn):
        await conn.execute(legacy._agent_spawns_ddl())
        await conn.execute(
            "INSERT INTO schema_migrations (name) VALUES ($1)", legacy._AGENT_SPAWNS
        )
