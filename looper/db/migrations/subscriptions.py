"""The subscriptions migration, in its released execution order."""

name = "subscriptions"
order = 280
pending_order = 170


async def pending(conn):
    from .. import connection as legacy

    return await legacy._subscriptions_pending(conn)


async def apply(conn, *, sql, state):
    from .. import connection as legacy

    if await legacy._subscriptions_pending(conn):
        await conn.execute(legacy._subscriptions_ddl())
        await conn.execute(
            "INSERT INTO schema_migrations (name) VALUES ($1)", legacy._SUBSCRIPTIONS
        )
