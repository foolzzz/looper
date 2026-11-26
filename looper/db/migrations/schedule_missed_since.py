"""The schedule_missed_since migration, in its released execution order."""

name = "schedule_missed_since"
order = 130
pending_order = 130


async def pending(conn):
    from .. import connection as legacy

    return await legacy._schedule_missed_since_pending(conn)


async def apply(conn, *, sql, state) -> bool:
    did_work = False
    from .. import connection as legacy

    if await legacy._schedule_missed_since_pending(conn):
        did_work = True
        await conn.execute(
            "ALTER TABLE schedule_runs ADD COLUMN missed_since TIMESTAMPTZ"
        )
        await conn.execute(
            "INSERT INTO schema_migrations (name) VALUES ($1)",
            legacy._SCHEDULE_MISSED_SINCE,
        )
    return did_work
