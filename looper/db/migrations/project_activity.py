"""The project_activity migration, in its released execution order."""

name = "project_activity"
order = 410
pending_order = 150


async def pending(conn):
    from .. import connection as legacy

    return await legacy._project_activity_pending(conn)


async def apply(conn, *, sql, state):
    from .. import connection as legacy

    if await legacy._project_activity_pending(conn):
        await conn.execute(legacy._PROJECT_ACTIVITY_DDL)
        await conn.execute(
            "INSERT INTO schema_migrations (name) VALUES ($1)", legacy._PROJECT_ACTIVITY
        )
