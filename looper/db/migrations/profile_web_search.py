"""The profile_web_search migration, in its released execution order."""

name = "profile_web_search"
order = 40
pending_order = 90


async def pending(conn):
    from .. import connection as legacy

    return await legacy._profile_web_search_pending(conn)


async def apply(conn, *, sql, state):
    from .. import connection as legacy

    if await legacy._profile_web_search_pending(conn):
        await conn.execute(
            "ALTER TABLE agent_profiles ADD COLUMN web_search BOOLEAN NOT NULL DEFAULT true"
        )
        await conn.execute(
            "INSERT INTO schema_migrations (name) VALUES ($1)",
            legacy._PROFILE_WEB_SEARCH,
        )
