"""The verification_paths migration, in its released execution order."""

name = "verification_paths"
order = 500
pending_order = 520


async def pending(conn):
    from .. import connection as legacy

    return await legacy._verification_paths_pending(conn)


async def apply(conn, *, sql, state) -> bool:
    did_work = False
    from .. import connection as legacy

    if await legacy._verification_paths_pending(conn):
        did_work = True
        await conn.execute(
            "ALTER TABLE projects ADD COLUMN verification_paths TEXT[] "
            "CHECK (cardinality(verification_paths) <= 20)"
        )
        await conn.execute(
            "INSERT INTO schema_migrations (name) VALUES ($1)",
            legacy._VERIFICATION_PATHS,
        )
    return did_work
