"""The schedule_notify_format migration, in its released execution order."""

name = "schedule_notify_format"
order = 90
pending_order = 20


async def pending(conn):
    from .. import connection as legacy

    return await legacy._schedule_notify_format_pending(conn)


async def apply(conn, *, sql, state) -> bool:
    did_work = False
    from .. import connection as legacy

    if await legacy._schedule_notify_format_pending(conn):
        did_work = True
        await conn.execute(
            "ALTER TABLE schedules ADD COLUMN notify_format TEXT NOT NULL DEFAULT 'report' "
            "CHECK (notify_format IN ('report', 'deliverable'))"
        )
        await conn.execute(
            "ALTER TABLE schedules ADD CONSTRAINT schedules_command_notify_format_check "
            "CHECK (kind <> 'command' OR notify_format = 'report')"
        )
        await conn.execute(
            "INSERT INTO schema_migrations (name) VALUES ($1)",
            legacy._SCHEDULE_NOTIFY_FORMAT,
        )
    return did_work
