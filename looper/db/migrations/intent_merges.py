"""The intent_merges migration, in its released execution order."""

name = "intent_merges"
order = 520
pending_order = 280


async def pending(conn):
    from .. import connection as legacy

    return await legacy._intent_merges_pending(conn)


async def apply(conn, *, sql, state) -> bool:
    did_work = False
    from .. import connection as legacy

    if await legacy._intent_merges_pending(conn):
        did_work = True
        await conn.execute(legacy._intent_merges_ddl())
        await conn.execute(
            "INSERT INTO schema_migrations (name) VALUES ($1) ON CONFLICT DO NOTHING",
            legacy._INTENT_MERGES,
        )
    return did_work
