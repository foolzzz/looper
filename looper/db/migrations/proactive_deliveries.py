"""The proactive_deliveries migration, in its released execution order."""

name = "proactive_deliveries"
order = 590
pending_order = 221


async def pending(conn):
    from .. import connection as legacy

    return await legacy._proactive_deliveries_pending(conn)


async def apply(conn, *, sql, state) -> bool:
    did_work = False
    from .. import connection as legacy

    if await legacy._proactive_deliveries_pending(conn):
        did_work = True
        await conn.execute(legacy._proactive_deliveries_ddl())
        await conn.execute(
            "INSERT INTO schema_migrations (name) VALUES ($1)",
            legacy._PROACTIVE_DELIVERIES,
        )
    return did_work
