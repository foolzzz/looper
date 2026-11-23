"""The channel_outbox migration, preserving the released upgrade path."""

name = "channel_outbox"
order = 598
pending_order = 225


async def pending(conn):
    from .. import connection as legacy

    return await legacy._channel_outbox_pending(conn)


async def apply(conn, *, sql, state):
    from .. import connection as legacy

    if await pending(conn):
        await conn.execute(legacy._channel_outbox_ddl())
        await conn.execute("INSERT INTO schema_migrations (name) VALUES ($1) ON CONFLICT DO NOTHING", legacy._CHANNEL_OUTBOX)
