"""A new migration file supplies fresh and upgrade paths without shared edits."""

import asyncio
import sys

import pytest

from looper.db import bridge_upgrade, connection, migrations
from tests.bridge_schema_support import structure, witness
from tests.db_support import DSN, fresh_connection

pytestmark = pytest.mark.skipif(not DSN, reason="requires disposable LOOPER_TEST_DATABASE_URL")


_NEW_MIGRATION = '''\
name = "registry_example_items"
order = 10000
pending_order = 10000
fresh_ddl = "CREATE TABLE IF NOT EXISTS registry_example_items (id BIGSERIAL PRIMARY KEY, note TEXT NOT NULL DEFAULT '');"


async def pending(conn):
    if await conn.fetchval("SELECT EXISTS (SELECT 1 FROM schema_migrations WHERE name = $1)", name):
        return False
    return not await conn.fetchval("SELECT to_regclass('registry_example_items') IS NOT NULL")


async def apply(conn, *, sql, state):
    if await pending(conn):
        await conn.execute(fresh_ddl)
        await conn.execute("INSERT INTO schema_migrations (name) VALUES ($1)", name)
'''


def test_new_migration_file_upgrades_and_initializes_the_same_schema(tmp_path, monkeypatch):
    module_name = "registry_test_example_items"
    (tmp_path / f"{module_name}.py").write_text(_NEW_MIGRATION)
    monkeypatch.setattr(migrations, "__path__", [*migrations.__path__, str(tmp_path)])

    async def test():
        async with fresh_connection() as upgraded, fresh_connection() as fresh:
            # This installation was initialized before the new file was shipped.
            await connection.ensure_schema(upgraded)
            assert not await connection.schema_change_pending(upgraded)
            assert not await upgraded.fetchval("SELECT to_regclass('registry_example_items') IS NOT NULL")
            before, shape = await witness(upgraded), await structure(upgraded)

            discovered = migrations.discover()
            assert [migration.name for migration in discovered][-1] == "registry_example_items"
            monkeypatch.setattr(connection, "_MIGRATIONS", discovered)
            monkeypatch.setattr(connection, "_PENDING_MIGRATIONS", migrations.pending_migrations(discovered))
            monkeypatch.setattr(connection, "_ONLINE_STEPS", migrations.online_steps(discovered))

            # Backup admission discovers the addition but changes neither rows nor catalog.
            assert await connection.schema_change_pending(upgraded)
            assert await witness(upgraded) == before
            assert await structure(upgraded) == shape
            await connection.ensure_schema(upgraded)
            await connection.ensure_schema(fresh)
            assert await structure(upgraded) == await structure(fresh)
            assert {row["name"] for row in await upgraded.fetch("SELECT name FROM schema_migrations")} == {
                bridge_upgrade.BASELINE, "registry_example_items"}
            assert {row["name"] for row in await fresh.fetch("SELECT name FROM schema_migrations")} == {
                bridge_upgrade.BASELINE}

            for conn in (upgraded, fresh):
                assert await conn.fetchval("INSERT INTO registry_example_items (note) VALUES ('kept') RETURNING id") == 1
                saved = await witness(conn)
                assert not await connection.schema_change_pending(conn)
                await connection.ensure_schema(conn)
                assert await witness(conn) == saved
                assert not await connection.schema_change_pending(conn)

    try:
        asyncio.run(asyncio.wait_for(test(), 30))
    finally:
        sys.modules.pop(f"{migrations.__name__}.{module_name}", None)


@pytest.mark.parametrize("migration_name,rollback", [
    ("skill_proposal_feedback", "ALTER TABLE skill_proposal_events DROP COLUMN told_at"),
    ("mcp_tool_levels", "DROP TABLE mcp_tool_levels CASCADE"),
    ("failure_typing", "ALTER TABLE llm_records DROP COLUMN failure_class, DROP COLUMN failure_cause"),
    ("memory_candidates", "DROP TABLE memory_candidates, memory_candidate_revisions, memory_candidate_events, "
                          "memory_candidate_sources, memory_posts, memory_tombstones CASCADE"),
    ("channel_outbox", "DROP TABLE channel_outbox CASCADE"),
    ("manager_sessions", "DROP TABLE manager_sessions CASCADE"),
    ("provider_limit_windows", "DROP TABLE provider_limit_windows CASCADE"),
    ("provider_admission", "DROP TABLE provider_admission, provider_admission_events CASCADE"),
])
def test_merged_migrations_remain_discoverable_and_upgrade(migration_name, rollback):
    async def test():
        async with fresh_connection() as conn:
            await connection.ensure_schema(conn)
            await conn.execute(rollback)
            await conn.execute("DELETE FROM schema_migrations WHERE name = ANY($1)",
                               [migration_name, f"{migration_name}_online"])
            assert await connection.schema_change_pending(conn)
            await connection.ensure_schema(conn)
            assert not await connection.schema_change_pending(conn)
            assert await conn.fetchval("SELECT EXISTS (SELECT 1 FROM schema_migrations WHERE name=$1)",
                                       migration_name)

    asyncio.run(test())


@pytest.mark.parametrize("missing_marker", [False, True])
def test_room_delivery_resumes_validation_without_replacing_constraint(missing_marker):
    async def test():
        async with fresh_connection() as conn:
            await connection.ensure_schema(conn)
            await conn.execute("ALTER TABLE room_messages DROP CONSTRAINT room_messages_kind_check, "
                "ADD CONSTRAINT room_messages_kind_check CHECK (kind IN "
                "('info','question','answer','interface','announce','notice','escalation'))")
            async with conn.transaction():
                await connection.ensure_schema(conn)
            constraint = await conn.fetchrow("SELECT oid,convalidated FROM pg_constraint "
                "WHERE conrelid='room_messages'::regclass AND conname='room_messages_kind_check'")
            assert not constraint["convalidated"]
            if missing_marker:
                await conn.execute("DELETE FROM schema_migrations WHERE name='subagent_room_delivery'")
            await connection.ensure_schema(conn)
            validated = await conn.fetchrow("SELECT oid,convalidated FROM pg_constraint "
                "WHERE conrelid='room_messages'::regclass AND conname='room_messages_kind_check'")
            assert validated["oid"] == constraint["oid"] and validated["convalidated"]
            assert await conn.fetchval("SELECT count(*) FROM schema_migrations WHERE name IN "
                                      "('subagent_room_delivery','subagent_room_delivery_online')") == 2
            assert not await connection.schema_change_pending(conn)

    asyncio.run(test())
