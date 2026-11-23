"""Register proactive deliveries after integrating the migration registry."""

name = "proactive_deliveries"
order = 590
pending_order = 225


async def pending(conn):
    from .. import connection as legacy

    return await legacy._proactive_deliveries_pending(conn)


async def apply(conn, *, sql, state):
    from .. import connection as legacy

    if await pending(conn):
        await conn.execute(legacy._proactive_deliveries_ddl())
        await conn.execute("INSERT INTO schema_migrations (name) VALUES ($1)", name)
