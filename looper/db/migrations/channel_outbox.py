"""The channel_outbox migration, in its released execution order."""

name = "channel_outbox"
order = 630
pending_order = 222


async def pending(conn):
    from .. import connection as legacy

    return await legacy._channel_outbox_pending(conn)


async def apply(conn, *, sql, state):
    from .. import connection as legacy

    if await legacy._channel_outbox_pending(conn):
        await conn.execute(legacy._channel_outbox_ddl())
        await conn.execute(
            "INSERT INTO schema_migrations (name) VALUES ($1) ON CONFLICT DO NOTHING",
            legacy._CHANNEL_OUTBOX,
        )
