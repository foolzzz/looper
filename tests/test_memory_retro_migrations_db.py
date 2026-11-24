"""Retro migrations repair missing objects and preserve the role union."""

import asyncio

import pytest

from looper.db import connection, migrations
from looper.db.migrations import memory_retro_queue as queue, memory_retro_role as role
from tests.bridge_schema_support import structure
from tests.db_support import DSN, fresh_connection

pytestmark = pytest.mark.skipif(not DSN, reason="requires disposable LOOPER_TEST_DATABASE_URL")


def test_fresh_upgrade_repair_and_online_role_union():
    async def check():
        async with fresh_connection() as old, fresh_connection() as fresh:
            previous = tuple(m for m in connection._MIGRATIONS if m.name not in {role.name,queue.name})
            await old.execute(migrations.schema_sql(connection.SCHEMA_PATH.read_text(), previous))
            from looper.db.bridge_upgrade import BASELINE
            await old.execute("INSERT INTO registry_schema_version(id,version) VALUES(TRUE,$1)",
                              connection.REGISTRY_SCHEMA_VERSION)
            await old.execute("INSERT INTO schema_migrations(name) VALUES($1)", BASELINE)
            assert await role.pending(old)
            assert await queue.pending(old)
            await connection.ensure_schema(old)
            await connection.ensure_schema(fresh)
            assert await structure(old) == await structure(fresh)
            assert {r[0] for r in await fresh.fetch("SELECT name FROM schema_migrations")} == {BASELINE}
            await old.execute("ALTER TABLE memory_retro_runs DROP COLUMN lease_until")
            assert await queue.pending(old)
            await connection.ensure_schema(old)
            assert not await queue.pending(old)
            await old.execute("DROP TABLE memory_retro_task_refs")
            assert await queue.pending(old)
            await connection.ensure_schema(old)
            assert not await queue.pending(old)
            for table, column, constraint in role._CHECKS:
                definition = await old.fetchval("SELECT pg_get_constraintdef(oid) FROM pg_constraint "
                                                "WHERE conrelid=to_regclass($1) AND conname=$2", table, constraint)
                assert "memory_retro" in definition and "memory_draft" in definition
                # Another feature may have widened this exact CHECK since the baseline.
                altered = definition.replace("memory_retro", "synthetic_role")
                await old.execute(f"ALTER TABLE {table} DROP CONSTRAINT {constraint}")
                await old.execute(f"ALTER TABLE {table} ADD CONSTRAINT {constraint} {altered}")
                assert await role.pending(old)
                await connection.ensure_schema(old)
                repaired = await old.fetchval("SELECT pg_get_constraintdef(oid) FROM pg_constraint "
                                              "WHERE conrelid=to_regclass($1) AND conname=$2", table, constraint)
                assert all(value in repaired for value in ("memory_retro", "memory_draft", "synthetic_role"))
    asyncio.run(check())
