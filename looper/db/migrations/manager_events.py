"""The manager_events migration, in its released execution order."""

name = "manager_events"
order = 330
pending_order = 230


async def pending(conn):
    from .. import connection as legacy

    return await legacy._manager_events_pending(conn)


async def apply(conn, *, sql, state) -> bool:
    did_work = False
    from .. import connection as legacy

    if await legacy._manager_events_pending(conn):
        did_work = True
        await conn.execute(legacy._manager_events_ddl())
        await conn.execute(
            "INSERT INTO schema_migrations (name) VALUES ($1)", legacy._MANAGER_EVENTS
        )
        # The block added the outbox's column too: its index is built online, as for a later database.
        await conn.execute(
            "INSERT INTO schema_migrations (name) VALUES ($1)",
            legacy._SCHEDULE_NOTIFICATION_EVENTS,
        )
    return did_work
