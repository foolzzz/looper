"""The schedule_deferred_by_drain migration, in its released execution order."""

name = "schedule_deferred_by_drain"
order = 140
pending_order = 140


async def pending(conn):
    from .. import connection as legacy

    return await legacy._schedule_deferred_by_drain_pending(conn)


async def apply(conn, *, sql, state):
    from .. import connection as legacy

    if await legacy._schedule_deferred_by_drain_pending(conn):
        await conn.execute(
            "ALTER TABLE schedule_runs ADD COLUMN deferred_by_drain BOOLEAN NOT NULL DEFAULT false"
        )
        await conn.execute(
            "INSERT INTO schema_migrations (name) VALUES ($1)",
            legacy._SCHEDULE_DEFERRED_BY_DRAIN,
        )
