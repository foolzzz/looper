"""The message_leads migration, in its released execution order."""

name = "message_leads"
order = 510
pending_order = 270


async def pending(conn):
    from .. import connection as legacy

    return await legacy._message_leads_pending(conn)


async def apply(conn, *, sql, state):
    from .. import connection as legacy

    if await legacy._message_leads_pending(conn):
        await conn.execute(legacy._message_leads_ddl())
        await conn.execute(
            "INSERT INTO schema_migrations (name) VALUES ($1)", legacy._MESSAGE_LEADS
        )
