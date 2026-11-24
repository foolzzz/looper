"""The collection migration repairs missing objects and rejects incompatible ones."""
import asyncio

import pytest

from looper.db import connection, bridge_upgrade
from looper.db.migrations import discover
from looper.db.migrations import message_collection as migration
from tests.db_support import DSN, fresh_connection

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")


def test_fresh_upgrade_repair_and_online_source_extension():
    async def check():
        async with fresh_connection() as conn:
            assert migration.order == migration.pending_order == migration.online_order == 1950
            assert len([m for m in discover() if m.order == 1950]) == 1
            await connection.ensure_schema(conn)
            assert not await migration.pending(conn)
            assert [r[0] for r in await conn.fetch("SELECT name FROM schema_migrations")] == [bridge_upgrade.BASELINE]
            await conn.execute("DROP INDEX idx_source_parity_members_bucket")
            assert await migration.pending(conn)
            await connection.ensure_schema(conn)
            assert not await migration.pending(conn)
            await conn.execute("ALTER TABLE source_collection_settings DROP CONSTRAINT source_collection_settings_generation_check")
            assert await migration.pending(conn)
            await connection.ensure_schema(conn)
            assert not await migration.pending(conn)
            before = await conn.fetch("SELECT * FROM schema_migrations ORDER BY name")
            await connection.ensure_schema(conn)
            assert await conn.fetch("SELECT * FROM schema_migrations ORDER BY name") == before
            definition = await conn.fetchval("SELECT pg_get_constraintdef(oid) FROM pg_constraint WHERE conrelid='proactive_deliveries'::regclass AND conname='proactive_deliveries_source_check'")
            assert "message_stream_alert" in definition and "metrics_alert" in definition
    asyncio.run(check())


def test_incompatible_column_or_check_definition_is_rejected():
    async def check():
        async with fresh_connection() as conn:
            await connection.ensure_schema(conn)
            await conn.execute("ALTER TABLE source_collection_settings DROP CONSTRAINT source_collection_settings_generation_check")
            await conn.execute("ALTER TABLE source_collection_settings ADD CONSTRAINT source_collection_settings_generation_check CHECK (generation > 100)")
            with pytest.raises(RuntimeError, match="incompatible.*generation"):
                await migration.pending(conn)
    asyncio.run(check())


def test_upgrade_matches_fresh_and_online_validation_resumes_without_narrowing_sources():
    from looper.db.migrations import _widen_check
    from tests.bridge_schema_support import structure

    async def check():
        async with fresh_connection() as upgraded, fresh_connection() as fresh:
            await connection.ensure_schema(upgraded)
            await connection.ensure_schema(fresh)
            await upgraded.execute("DROP TABLE source_stream_alerts,source_parity_coverage,source_parity_members,source_parity_sessions,source_collection_settings")
            assert await migration.pending(upgraded)
            await connection.ensure_schema(upgraded)
            assert not await migration.pending(upgraded)
            assert await structure(upgraded) == await structure(fresh)
            assert await upgraded.fetchval("SELECT EXISTS(SELECT 1 FROM schema_migrations WHERE name=$1)", migration.name)
            # Another additive migration's value survives, and a process stop before validation is resumable.
            await upgraded.execute(_widen_check.ddl("proactive_deliveries", "source", "proactive_deliveries_source_check", ("future_alert",)))
            await migration.apply(upgraded, sql="", state="current")
            assert not await upgraded.fetchval("SELECT convalidated FROM pg_constraint WHERE conname='proactive_deliveries_source_check' AND connamespace=current_schema()::regnamespace")
            await connection.ensure_schema(upgraded)
            definition = await upgraded.fetchval("SELECT pg_get_constraintdef(oid) FROM pg_constraint WHERE conname='proactive_deliveries_source_check' AND connamespace=current_schema()::regnamespace")
            assert "future_alert" in definition and "message_stream_alert" in definition and "metrics_alert" in definition
            assert await upgraded.fetchval("SELECT convalidated FROM pg_constraint WHERE conname='proactive_deliveries_source_check' AND connamespace=current_schema()::regnamespace")
    asyncio.run(check())


@pytest.mark.parametrize("damage", [
    "ALTER TABLE source_collection_settings ALTER COLUMN generation TYPE TEXT USING generation::text",
    "ALTER TABLE source_collection_settings DROP CONSTRAINT source_collection_settings_source_id_fkey; "
    "ALTER TABLE source_collection_settings ADD CONSTRAINT source_collection_settings_source_id_fkey FOREIGN KEY(source_id) REFERENCES event_sources(id) ON DELETE RESTRICT",
    "DROP INDEX idx_source_parity_members_bucket; CREATE INDEX idx_source_parity_members_bucket ON source_parity_members(session_id,external_id)",
])
def test_incompatible_column_foreign_key_or_index_is_rejected(damage):
    async def check():
        async with fresh_connection() as conn:
            await connection.ensure_schema(conn)
            if "TYPE TEXT" in damage:
                await conn.execute("ALTER TABLE source_collection_settings DROP CONSTRAINT source_collection_settings_generation_check, ALTER COLUMN generation DROP DEFAULT")
            await conn.execute(damage)
            with pytest.raises(RuntimeError, match="incompatible"):
                await migration.pending(conn)
    asyncio.run(check())
