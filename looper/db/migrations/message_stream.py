"""The message_stream migration, in its released execution order."""

name = "message_stream"
order = 260
pending_order = 160


async def pending(conn):
    from .. import connection as legacy

    return await legacy._message_stream_pending(conn)


async def apply(conn, *, sql, state) -> bool:
    did_work = False
    from .. import connection as legacy

    if await legacy._message_stream_pending(conn):
        did_work = True
        await conn.execute(legacy._message_stream_ddl())
        await conn.execute(
            "INSERT INTO schema_migrations (name) VALUES ($1)", legacy._MESSAGE_STREAM
        )
    return did_work
