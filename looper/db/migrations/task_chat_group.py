"""The task_chat_group migration, in its released execution order."""

name = "task_chat_group"
order = 150
pending_order = 530


async def pending(conn):
    from .. import connection as legacy

    return await legacy._task_chat_group_pending(conn)


async def apply(conn, *, sql, state) -> bool:
    did_work = False
    from .. import connection as legacy

    if await legacy._task_chat_group_pending(conn):
        did_work = True
        await conn.execute(
            "ALTER TABLE tasks ADD COLUMN chat_group BOOLEAN NOT NULL DEFAULT false"
        )
        await conn.execute(
            "INSERT INTO schema_migrations (name) VALUES ($1)", legacy._TASK_CHAT_GROUP
        )
    return did_work
