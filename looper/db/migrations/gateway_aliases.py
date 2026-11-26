"""The gateway_aliases migration, in its released execution order."""

name = "gateway_aliases"
order = 250
pending_order = None


async def pending(conn):
    from .. import connection as legacy

    return await legacy._gateway_aliases_pending(conn)


async def apply(conn, *, sql, state) -> bool:
    did_work = False
    from .. import connection as legacy

    if await legacy._gateway_aliases_pending(conn):
        did_work = True
        await conn.execute(legacy._GATEWAY_ALIASES_DDL)
        await conn.execute(
            "INSERT INTO schema_migrations (name) VALUES ($1)", legacy._GATEWAY_ALIASES
        )
    return did_work
