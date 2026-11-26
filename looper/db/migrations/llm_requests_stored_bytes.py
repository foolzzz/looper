"""The llm_requests_stored_bytes migration, in its released execution order."""

name = "llm_requests_stored_bytes"
order = 240
pending_order = 420


async def pending(conn):
    from .. import connection as legacy

    return await legacy._llm_requests_stored_bytes_pending(conn)


async def apply(conn, *, sql, state) -> bool:
    did_work = False
    from .. import connection as legacy

    if await legacy._llm_requests_stored_bytes_pending(conn):
        did_work = True
        for table in ("llm_requests", "llm_request_parts"):
            await conn.execute(
                f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS stored_bytes BIGINT"
            )
            await conn.execute(
                f"UPDATE {table} AS row SET stored_bytes = octet_length(row::text) "
                "WHERE stored_bytes IS NULL"
            )
            await conn.execute(
                f"ALTER TABLE {table} ALTER COLUMN stored_bytes SET DEFAULT 0, "
                "ALTER COLUMN stored_bytes SET NOT NULL"
            )
        await conn.execute(
            "INSERT INTO schema_migrations (name) VALUES ($1)",
            legacy._LLM_REQUESTS_STORED_BYTES,
        )
    return did_work
