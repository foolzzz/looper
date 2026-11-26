"""The reminders migration, in its released execution order."""

name = "reminders"
order = 580
pending_order = 570


async def pending(conn):
    from .. import connection as legacy

    return await legacy._reminders_pending(conn)


async def apply(conn, *, sql, state) -> bool:
    did_work = False
    from .. import connection as legacy

    if await legacy._reminders_pending(conn):
        did_work = True
        await conn.execute(legacy._reminders_ddl())
        await conn.execute(
            "INSERT INTO schema_migrations (name) VALUES ($1)", legacy._REMINDERS
        )
    return did_work
