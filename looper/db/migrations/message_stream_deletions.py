"""The message_stream_deletions migration, in its released execution order."""

name = "message_stream_deletions"
order = 270
pending_order = 180


async def pending(conn):
    from .. import connection as legacy

    return await legacy._message_stream_deletions_pending(conn)


async def apply(conn, *, sql, state):
    from .. import connection as legacy

    if await legacy._message_stream_deletions_pending(conn):
        await conn.execute(
            "ALTER TABLE event_sources ADD COLUMN IF NOT EXISTS retention_last_at TIMESTAMPTZ"
        )
        await conn.execute(
            "ALTER TABLE source_runs ADD COLUMN IF NOT EXISTS skipped_by_deletion "
            "BIGINT NOT NULL DEFAULT 0"
        )
        await conn.execute(legacy._source_deletions_ddl())
        await conn.execute(
            "INSERT INTO schema_migrations (name) VALUES ($1)",
            legacy._MESSAGE_STREAM_DELETIONS,
        )
