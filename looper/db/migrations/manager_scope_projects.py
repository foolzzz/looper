"""The manager_scope_projects migration, in its released execution order."""

name = "manager_scope_projects"
order = 120
pending_order = 450


async def pending(conn):
    from .. import connection as legacy

    return await legacy._manager_scope_projects_pending(conn)


async def apply(conn, *, sql, state) -> bool:
    did_work = False
    from .. import connection as legacy

    if await legacy._manager_scope_projects_pending(conn):
        did_work = True
        for ddl in legacy._MANAGER_SCOPE_PROJECTS_DDL:
            await conn.execute(ddl)
        await conn.execute(
            "INSERT INTO schema_migrations (name) VALUES ($1)",
            legacy._MANAGER_SCOPE_PROJECTS,
        )
    return did_work
