"""Build the leads token index online on upgrades and in the fresh schema."""

from __future__ import annotations

import asyncpg

from . import OnlineStep

name = "leads_search"
order = 690
pending_order = 690

INDEX = "idx_message_leads_search"
INDEX_DEFINITION = "ON message_leads USING gin (array_to_tsvector(looper_search_tokens(summary || ' ' || detail)))"
fresh_ddl = f"CREATE INDEX IF NOT EXISTS {INDEX} {INDEX_DEFINITION};"


async def pending(conn: asyncpg.Connection) -> bool:
    if await conn.fetchval("SELECT EXISTS (SELECT 1 FROM schema_migrations WHERE name = $1)", name):
        return False
    return await conn.fetchval(
        "SELECT to_regclass('message_leads') IS NOT NULL AND to_regclass('idx_message_leads_search') IS NULL"
    )


async def apply(conn: asyncpg.Connection, *, sql: str, state: str) -> bool:
    did_work = False
    if await pending(conn):
        # search_tokens has already installed the tokenizer; leave the index
        # build to the resumable runner after the schema transaction commits.
        did_work = True
        await conn.execute("INSERT INTO schema_migrations (name) VALUES ($1)", name)
    return did_work


online_order = 150
online = (
    OnlineStep(name, "leads_search_online", indexes=((INDEX, INDEX_DEFINITION),)),
)
