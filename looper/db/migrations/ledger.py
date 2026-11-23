"""The ledger migration, in its released execution order."""

name = "ledger"
order = 310
pending_order = 210


async def pending(conn):
    from .. import connection as legacy

    return await legacy._ledger_pending(conn)


async def apply(conn, *, sql, state):
    from .. import connection as legacy

    if await legacy._ledger_pending(conn):
        await conn.execute(legacy._ledger_ddl())
        await conn.execute(
            "INSERT INTO schema_migrations (name) VALUES ($1)", legacy._LEDGER
        )
