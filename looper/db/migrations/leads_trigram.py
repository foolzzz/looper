"""Try pg_trgm once per installation and fill its optional leads index."""

from __future__ import annotations

import asyncpg

name = "leads_trigram"
order = 680
pending_order = 680

INDEX = "idx_message_leads_trigram"
# Extensions are shared across the per-test schemas of one database. Permission
# failure leaves full-text search available without literal/fuzzy candidates.
DDL = """
DO $$
BEGIN
    CREATE EXTENSION IF NOT EXISTS pg_trgm SCHEMA public;
EXCEPTION WHEN OTHERS THEN
    RAISE WARNING 'pg_trgm is not available (%): the leads full-text search matches without it', SQLERRM;
END $$;
"""


async def trigram_schema(conn: asyncpg.Connection) -> str | None:
    """Return the installed extension's quoted schema, wherever it lives."""
    return await conn.fetchval(
        "SELECT quote_ident(n.nspname) FROM pg_extension e "
        "JOIN pg_namespace n ON n.oid = e.extnamespace WHERE e.extname = 'pg_trgm'"
    )


async def extension_pending(conn: asyncpg.Connection) -> bool:
    if await conn.fetchval("SELECT EXISTS (SELECT 1 FROM schema_migrations WHERE name = $1)", name):
        return False
    return not await conn.fetchval("SELECT EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'pg_trgm')")


async def index_pending(conn: asyncpg.Connection) -> bool:
    return (
        await trigram_schema(conn) is not None
        and await conn.fetchval(
            "SELECT to_regclass('message_leads') IS NOT NULL AND to_regclass($1) IS NULL", INDEX
        )
    )


async def pending(conn: asyncpg.Connection) -> bool:
    return await extension_pending(conn) or await index_pending(conn)


async def apply(conn: asyncpg.Connection, *, sql: str, state: str) -> None:
    if await extension_pending(conn):
        await conn.execute(DDL)
        # A fresh database records only its baseline. If the attempt failed,
        # its next start gets one more attempt and records it even on failure.
        if state != "empty":
            await conn.execute("INSERT INTO schema_migrations (name) VALUES ($1)", name)
    # This is intentionally independent of the extension-attempt marker: an
    # extension installed later, or an index dropped later, must get its index.
    if await index_pending(conn):
        await conn.execute(
            f"CREATE INDEX IF NOT EXISTS {INDEX} ON message_leads USING gin "
            f"(lower(summary || ' ' || detail) {await trigram_schema(conn)}.gin_trgm_ops)"
        )
