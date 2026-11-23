"""The decision_snapshots migration, in its released execution order."""

name = "decision_snapshots"
order = 400
pending_order = 370


async def pending(conn):
    from .. import connection as legacy

    return await legacy._decision_snapshots_pending(conn)


async def apply(conn, *, sql, state):
    from .. import connection as legacy

    if await legacy._decision_snapshots_pending(conn):
        await conn.execute(legacy._decision_snapshots_ddl())
        await conn.execute(
            "INSERT INTO schema_migrations (name) VALUES ($1)",
            legacy._DECISION_SNAPSHOTS,
        )
