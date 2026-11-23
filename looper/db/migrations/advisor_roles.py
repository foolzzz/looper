"""The advisor_roles migration, in its released execution order."""

name = "advisor_roles"
order = 190
pending_order = 360


async def pending(conn):
    from .. import connection as legacy

    return await legacy._advisor_roles_pending(conn)


async def apply(conn, *, sql, state):
    from .. import connection as legacy

    if await legacy._advisor_roles_pending(conn):
        await conn.execute(legacy._ADVISOR_ROLES_DDL)
        await conn.execute(
            "INSERT INTO schema_migrations (name) VALUES ($1)", legacy._ADVISOR_ROLES
        )
