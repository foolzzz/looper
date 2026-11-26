"""The processor_role migration, in its released execution order."""

name = "processor_role"
order = 160
pending_order = 540


async def pending(conn):
    from .. import connection as legacy

    return await legacy._processor_role_pending(conn)


async def apply(conn, *, sql, state) -> bool:
    did_work = False
    from .. import connection as legacy

    if await legacy._processor_role_pending(conn):
        did_work = True
        await conn.execute(legacy._PROCESSOR_ROLE_DDL)
        await conn.execute(
            "INSERT INTO schema_migrations (name) VALUES ($1)", legacy._PROCESSOR_ROLE
        )
    return did_work
