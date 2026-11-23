"""The subscription_lookback migration, in its released execution order."""

name = "subscription_lookback"
order = 300
pending_order = 200


async def pending(conn):
    from .. import connection as legacy

    return await legacy._subscription_lookback_pending(conn)


async def apply(conn, *, sql, state):
    from .. import connection as legacy

    if await legacy._subscription_lookback_pending(conn):
        await conn.execute(legacy._SUBSCRIPTION_LOOKBACK_DDL)
        await conn.execute(
            "INSERT INTO schema_migrations (name) VALUES ($1)",
            legacy._SUBSCRIPTION_LOOKBACK,
        )
