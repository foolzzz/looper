"""The memory_candidates migration, in its released execution order."""

name = "memory_candidates"
order = 620
pending_order = 323


async def pending(conn):
    from .. import connection as legacy

    return await legacy._memory_candidates_pending(conn)


async def apply(conn, *, sql, state):
    from .. import connection as legacy

    if await legacy._memory_candidates_pending(conn):
        await conn.execute(legacy._memory_candidates_ddl())
        await conn.execute(
            "INSERT INTO schema_migrations (name) VALUES ($1)",
            legacy._MEMORY_CANDIDATES,
        )
