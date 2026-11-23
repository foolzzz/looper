"""The provider_enabled migration, in its released execution order."""

name = "provider_enabled"
order = 440
pending_order = 460


async def pending(conn):
    from .. import connection as legacy

    return await legacy._provider_enabled_pending(conn)


async def apply(conn, *, sql, state):
    from .. import connection as legacy

    if await legacy._provider_enabled_pending(conn):
        await conn.execute(
            "ALTER TABLE llm_providers ADD COLUMN enabled BOOLEAN NOT NULL DEFAULT true"
        )
        await conn.execute(
            "INSERT INTO schema_migrations (name) VALUES ($1)", legacy._PROVIDER_ENABLED
        )
