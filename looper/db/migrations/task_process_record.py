"""The task_process_record migration, in its released execution order."""

name = "task_process_record"
order = 100
pending_order = 120


async def pending(conn):
    from .. import connection as legacy

    return await legacy._task_process_record_pending(conn)


async def apply(conn, *, sql, state) -> bool:
    did_work = False
    from .. import connection as legacy

    if await legacy._task_process_record_pending(conn):
        did_work = True
        await conn.execute(
            "ALTER TABLE tasks ADD COLUMN process_record JSONB "
            "CHECK (jsonb_typeof(process_record) = 'object')"
        )
        await conn.execute(
            "INSERT INTO schema_migrations (name) VALUES ($1)",
            legacy._TASK_PROCESS_RECORD,
        )
    return did_work
