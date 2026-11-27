"""The two widenings keep every value another migration added, whichever of them ran on the database."""

from __future__ import annotations

import re

import pytest

from looper.db import connection, migrations
from tests.db_support import DSN, fresh_connection

pytestmark = pytest.mark.skipif(not DSN, reason="requires disposable LOOPER_TEST_DATABASE_URL")

BY_NAME = {module.name: module for module in migrations.discover()}


async def values(conn, table, column):
    definition = await conn.fetchval(
        "SELECT pg_get_constraintdef(c.oid) FROM pg_constraint c JOIN pg_attribute a ON a.attrelid = c.conrelid "
        "AND a.attnum = ANY (c.conkey) WHERE c.conrelid = to_regclass($1) AND c.contype = 'c' AND a.attname = $2 "
        "AND cardinality(c.conkey) = 1", table, column)
    return set(re.findall(r"'([a-z_]+)'", definition))


@pytest.mark.parametrize("name,table,column,added,others", [
    ("triage_source", "proactive_deliveries", "source", {"triage"}, {"morning_brief", "idle_check"}),
    ("triage_handoff_reasons", "manager_events", "reason", {"delivery_deferred", "daily_cap"},
     {"morning_brief", "idle_check", "brief_failed"}),
    ("main_session_egress", "manager_events", "reason", {"withheld_not_operator"}, {"future_reason"}),
    ("completion_session_allowlist", "manager_events", "reason", {"session_allowlist"}, {"future_reason"}),
    ("main_session_egress", "manager_events", "kind", {"autonomous"}, {"future_kind"}),
    ("manager_session_client_kind", "manager_sessions", "kind", {"client"}, {"future_kind"}),
])
def test_a_widening_adds_its_values_to_the_check_the_database_has_now(name, table, column, added, others):
    import asyncio

    async def check():
        async with fresh_connection() as conn:
            await connection.ensure_schema(conn)
            module = BY_NAME[name]
            assert added <= await values(conn, table, column) and not await module.pending(conn)
            current = await values(conn, table, column)
            # A database where other migrations added values, and this one has not run yet.
            check_name = f"{table}_{column}_check"
            old = sorted((current - added) | others)
            await conn.execute(f"ALTER TABLE {table} DROP CONSTRAINT {check_name}")
            await conn.execute(f"ALTER TABLE {table} ADD CONSTRAINT {check_name} CHECK ({column} IN "
                               f"({', '.join(repr(v) for v in old)})) NOT VALID")
            await conn.execute("DELETE FROM schema_migrations WHERE name = ANY($1::text[])",
                               [name, module.online[0].marker])
            assert await module.pending(conn)
            await module.apply(conn, sql="", state="existing")
            assert await values(conn, table, column) == set(old) | added
            assert not await module.pending(conn)
            assert await conn.fetchval("SELECT EXISTS (SELECT 1 FROM schema_migrations WHERE name = $1)", name)
            assert not await conn.fetchval("SELECT EXISTS (SELECT 1 FROM schema_migrations WHERE name = $1)",
                                           module.online[0].marker)
            # Run again: nothing to widen, nothing changes.
            await module.apply(conn, sql="", state="existing")
            assert await values(conn, table, column) == set(old) | added
            valid = await conn.fetchval("SELECT convalidated FROM pg_constraint WHERE conname = $1 "
                                        "AND connamespace = current_schema()::regnamespace", check_name)
            assert valid is False  # validated by the online step after commit

    asyncio.run(check())
