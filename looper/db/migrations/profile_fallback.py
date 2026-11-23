"""The profile_fallback migration, in its released execution order."""

name = "profile_fallback"
order = 30
pending_order = 80


async def pending(conn):
    from .. import connection as legacy

    return await legacy._profile_fallback_pending(conn)


async def apply(conn, *, sql, state):
    from .. import connection as legacy

    if await legacy._profile_fallback_pending(conn):
        await conn.execute(
            "ALTER TABLE agent_profiles ADD COLUMN fallback_profile_id TEXT "
            "REFERENCES agent_profiles(id) ON DELETE RESTRICT"
        )
        # Use the fresh schema's canonical check so upgraded catalogs match it.
        constraint = legacy.re.search(
            r"CONSTRAINT task_journal_kind_check (CHECK \(kind IN \(.*?\)\))",
            sql,
            legacy.re.S,
        ).group(1)
        await conn.execute(
            "ALTER TABLE task_journal DROP CONSTRAINT task_journal_kind_check"
        )
        await conn.execute(
            "ALTER TABLE task_journal ADD CONSTRAINT task_journal_kind_check "
            + constraint
        )
        await conn.execute(
            "INSERT INTO schema_migrations (name) VALUES ($1)", legacy._PROFILE_FALLBACK
        )
