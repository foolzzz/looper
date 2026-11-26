"""The llm_requests migration, in its released execution order."""

name = "llm_requests"
order = 210
pending_order = 390


async def pending(conn):
    from .. import connection as legacy

    return await legacy._llm_requests_pending(conn)


async def apply(conn, *, sql, state) -> bool:
    did_work = False
    from .. import connection as legacy

    if await legacy._llm_requests_pending(conn):
        did_work = True
        await conn.execute(legacy._LLM_REQUESTS_DDL)
        await conn.execute(
            "INSERT INTO schema_migrations (name) VALUES ($1)", legacy._LLM_REQUESTS
        )
    return did_work
