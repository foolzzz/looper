"""The llm_requests_translation migration, in its released execution order."""

name = "llm_requests_translation"
order = 220
pending_order = 400


async def pending(conn):
    from .. import connection as legacy

    return await legacy._llm_requests_translation_pending(conn)


async def apply(conn, *, sql, state) -> bool:
    did_work = False
    from .. import connection as legacy

    if await legacy._llm_requests_translation_pending(conn):
        did_work = True
        await conn.execute(
            "ALTER TABLE llm_requests ADD COLUMN IF NOT EXISTS translation JSONB"
        )
        await conn.execute(
            "INSERT INTO schema_migrations (name) VALUES ($1)",
            legacy._LLM_REQUESTS_TRANSLATION,
        )
    return did_work
