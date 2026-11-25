"""Allow the morning brief itself as a proactive delivery source."""

import re

import asyncpg

name = "morning_brief_kind"
order = 720
pending_order = 720
_SOURCES = (
    "task_notice", "completion", "progress", "gate", "gate_reminder", "subscription", "intent", "schedule",
    "advisor_alert", "autonomous", "user_reminder", "morning_brief")
_CHECK = "proactive_deliveries_source_check"
_SOURCE_VALUES = ", ".join(f"'{source}'" for source in _SOURCES)
_ADD_CHECK = f"ALTER TABLE proactive_deliveries ADD CONSTRAINT {_CHECK} CHECK (source IN ({_SOURCE_VALUES}))"
# The frozen baseline supplies this canonical check on fresh databases.
fresh_ddl = f"ALTER TABLE proactive_deliveries DROP CONSTRAINT {_CHECK};\n{_ADD_CHECK};"


async def _source_row(conn: asyncpg.Connection):
    """The CHECK on ``proactive_deliveries.source`` alone. Its name is PostgreSQL's; a check that only mentions a
    source value in some other column is not this one."""
    if not await conn.fetchval("SELECT to_regclass('proactive_deliveries') IS NOT NULL"):
        return None
    return await conn.fetchrow(
        "SELECT c.conname, pg_get_constraintdef(c.oid) AS definition FROM pg_constraint c "
        "JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = ANY (c.conkey) "
        "WHERE c.conrelid = 'proactive_deliveries'::regclass AND c.contype = 'c' "
        "AND a.attname = 'source' AND a.attnum > 0 AND NOT a.attisdropped "
        "AND cardinality(c.conkey) = 1 ORDER BY c.conname LIMIT 1")


async def pending(conn: asyncpg.Connection) -> bool:
    """A database that already applied ``proactive_deliveries`` does not replay its CREATE, so the source check
    is widened when it does not yet name ``morning_brief``. If the table is absent, its earlier migration creates
    it before this migration applies."""
    if not await conn.fetchval("SELECT to_regclass('proactive_deliveries') IS NOT NULL"):
        return False
    row = await _source_row(conn)
    return row is None or "morning_brief" not in row["definition"]


async def apply(conn, *, sql, state) -> bool:
    did_work = False
    if await pending(conn):
        did_work = True
        row = await _source_row(conn)
        if row is not None and re.fullmatch(r"[A-Za-z0-9_]+", row["conname"]):
            await conn.execute(f"ALTER TABLE proactive_deliveries DROP CONSTRAINT {row['conname']}")
        await conn.execute(_ADD_CHECK + " NOT VALID")
        await conn.execute(f"ALTER TABLE proactive_deliveries VALIDATE CONSTRAINT {_CHECK}")
        if state != "empty":
            await conn.execute(
                "INSERT INTO schema_migrations (name) VALUES ($1) ON CONFLICT DO NOTHING", name)
    return did_work
