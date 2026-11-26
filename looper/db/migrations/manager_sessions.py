"""The manager_sessions migration, in its released execution order."""

name = "manager_sessions"
order = 640
pending_order = 571


async def pending(conn):
    from .. import connection as legacy

    return await legacy._manager_sessions_pending(conn)


async def apply(conn, *, sql, state) -> bool:
    did_work = False
    from .. import connection as legacy

    if await legacy._manager_sessions_pending(conn):
        did_work = True
        await conn.execute(legacy._manager_sessions_ddl())
        await conn.execute(
            "INSERT INTO schema_migrations (name) VALUES ($1)", legacy._MANAGER_SESSIONS
        )
    return did_work
