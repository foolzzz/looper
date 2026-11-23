"""The llm_models_chat_dialect migration, in its released execution order."""

name = "llm_models_chat_dialect"
order = 230
pending_order = 410


async def pending(conn):
    from .. import connection as legacy

    return await legacy._llm_models_chat_dialect_pending(conn)


async def apply(conn, *, sql, state):
    from .. import connection as legacy

    if await legacy._llm_models_chat_dialect_pending(conn):
        await conn.execute(
            "ALTER TABLE llm_models ADD COLUMN chat_dialect TEXT "
            "CHECK (chat_dialect IS NULL OR protocol = 'openai-chat')"
        )
        await conn.execute(
            "INSERT INTO schema_migrations (name) VALUES ($1)",
            legacy._LLM_MODELS_CHAT_DIALECT,
        )
