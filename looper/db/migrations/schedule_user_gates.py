"""The schedule_user_gates migration, in its released execution order."""

name = "schedule_user_gates"
order = 20
pending_order = 10


async def pending(conn):
    from .. import connection as legacy

    return await legacy._schedule_user_gates_pending(conn)


async def apply(conn, *, sql, state) -> bool:
    did_work = False
    from .. import connection as legacy

    if await legacy._schedule_user_gates_pending(conn):
        did_work = True
        await conn.execute(
            "ALTER TABLE schedules ADD COLUMN IF NOT EXISTS user_gates "
            "TEXT NOT NULL DEFAULT 'off' CHECK (user_gates IN ('off', 'on'))"
        )
        await conn.execute(
            "INSERT INTO schema_migrations (name) VALUES ($1)",
            legacy._SCHEDULE_USER_GATES,
        )
    return did_work
