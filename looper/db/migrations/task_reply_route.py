"""The task_reply_route migration, in its released execution order."""

name = "task_reply_route"
order = 110
pending_order = 110


async def pending(conn):
    from .. import connection as legacy

    return await legacy._task_reply_route_pending(conn)


async def apply(conn, *, sql, state):
    from .. import connection as legacy

    if await legacy._task_reply_route_pending(conn):
        await conn.execute(
            "ALTER TABLE tasks ADD COLUMN origin_reply_route JSONB "
            "CHECK (jsonb_typeof(origin_reply_route) = 'object')"
        )
        await conn.execute(
            "INSERT INTO schema_migrations (name) VALUES ($1)", legacy._TASK_REPLY_ROUTE
        )
