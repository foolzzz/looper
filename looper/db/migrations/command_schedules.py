"""The command_schedules migration, in its released execution order."""

name = "command_schedules"
order = 80
pending_order = 100


async def pending(conn):
    from .. import connection as legacy

    return await legacy._command_schedules_pending(conn)


async def apply(conn, *, sql, state) -> bool:
    did_work = False
    from .. import connection as legacy

    if await legacy._command_schedules_pending(conn):
        did_work = True
        for ddl in legacy._COMMAND_SCHEDULES_DDL:
            await conn.execute(ddl)
        await conn.execute(
            "INSERT INTO schema_migrations (name) VALUES ($1)",
            legacy._COMMAND_SCHEDULES,
        )
    return did_work
