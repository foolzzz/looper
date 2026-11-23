"""The project_advisor migration, in its released execution order."""

name = "project_advisor"
order = 200
pending_order = 380


async def pending(conn):
    from .. import connection as legacy

    return await legacy._project_advisor_pending(conn)


async def apply(conn, *, sql, state):
    from .. import connection as legacy

    if await legacy._project_advisor_pending(conn):
        await conn.execute(legacy._PROJECT_ADVISOR_DDL)
        await conn.execute(
            "INSERT INTO schema_migrations (name) VALUES ($1)", legacy._PROJECT_ADVISOR
        )
