"""The card_actions migration, in its released execution order."""

name = "card_actions"
order = 320
pending_order = 220


async def pending(conn):
    from .. import connection as legacy

    return await legacy._card_actions_pending(conn)


async def apply(conn, *, sql, state) -> bool:
    did_work = False
    from .. import connection as legacy

    if await legacy._card_actions_pending(conn):
        did_work = True
        await conn.execute(legacy._card_actions_ddl())
        await conn.execute(
            "INSERT INTO schema_migrations (name) VALUES ($1)", legacy._CARD_ACTIONS
        )
    return did_work
