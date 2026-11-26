"""The cursor_login migration, in its released execution order."""

name = "cursor_login"
order = 50
pending_order = 40


async def pending(conn):
    from .. import connection as legacy

    return await legacy._cursor_login_pending(conn)


async def apply(conn, *, sql, state) -> bool:
    did_work = False
    from .. import connection as legacy

    if await legacy._cursor_login_pending(conn):
        did_work = True
        await conn.execute(legacy._CURSOR_LOGIN_DDL)
        await conn.execute(
            "INSERT INTO schema_migrations (name) VALUES ($1)", legacy._CURSOR_LOGIN
        )
    return did_work
