"""Additive rule storage, immutable versions and evidence deletion boundaries."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from uuid import uuid4

import asyncpg
import pytest

from looper.db import bridge_upgrade, connection, migrations
from tests.db_support import DSN, fresh_connection, run_db

pytestmark = pytest.mark.skipif(not DSN, reason="requires disposable LOOPER_TEST_DATABASE_URL")
NOW = datetime(2026, 11, 20, tzinfo=timezone.utc)
TABLES = ("intent_rule_config", "intent_rules", "intent_rule_versions", "intent_rule_previews",
          "intent_rule_config_audit", "intent_rule_template_contracts", "intent_rule_hits",
          "intent_rule_condition_results", "intent_rule_hit_sources")


def test_intent_rule_migration_fresh_upgrade_and_reentry(monkeypatch):
    from looper.db.migrations import intent_rules as migration

    async def test():
        async with fresh_connection() as upgraded, fresh_connection() as fresh:
            old = tuple(m for m in connection._MIGRATIONS if m.name != migration.name)
            with monkeypatch.context() as patch:
                patch.setattr(connection, "_MIGRATIONS", old)
                patch.setattr(connection, "_PENDING_MIGRATIONS", migrations.pending_migrations(old))
                await connection.ensure_schema(upgraded)
            await upgraded.execute("INSERT INTO projects (id, name) VALUES ('kept', 'Kept')")
            await connection.ensure_schema(upgraded)
            await connection.ensure_schema(fresh)
            assert migration.order == migration.pending_order == 1880
            assert sum(m.order == 1880 for m in migrations.discover()) == 1
            for conn in (fresh, upgraded):
                assert not await migration.pending(conn)
                assert all([await conn.fetchval("SELECT to_regclass($1) IS NOT NULL", name) for name in TABLES])
                assert await conn.fetchval("SELECT count(*) FROM intent_rule_config") == 0
                assert await conn.fetchval("SELECT count(*) FROM intent_rules") == 0
                assert await conn.fetchval("SELECT value FROM system_settings WHERE key='intents.rules.enabled'") is None
                await connection.ensure_schema(conn)
                assert not await connection.schema_change_pending(conn)
            assert dict(await upgraded.fetchrow("SELECT auto_task_daily_cap, auto_task_config_version "
                                                "FROM projects WHERE id='kept'")) == {
                "auto_task_daily_cap": None, "auto_task_config_version": 0}
            assert {row["name"] for row in await fresh.fetch("SELECT name FROM schema_migrations")} == {
                bridge_upgrade.BASELINE}
            assert migration.name in {row["name"] for row in await upgraded.fetch("SELECT name FROM schema_migrations")}

    asyncio.run(test())


@pytest.mark.parametrize("damage", [
    "DROP TABLE intent_rule_hit_sources",
    "ALTER TABLE projects DROP COLUMN auto_task_daily_cap",
    "ALTER TABLE projects DROP CONSTRAINT projects_auto_task_config_version_check",
    "ALTER TABLE intent_rules DROP CONSTRAINT intent_rules_position_key",
    "ALTER TABLE intent_rule_versions DROP CONSTRAINT intent_rule_versions_pkey",
    "DROP INDEX idx_intent_rule_hits_created",
    "DROP TRIGGER intent_rule_hit_sources_drop ON intent_rule_hit_sources",
])
def test_rule_migration_detects_missing_objects_even_with_a_marker(damage):
    from looper.db.migrations import intent_rules as migration

    async def test(pool):
        async with pool.acquire() as conn, conn.transaction():
            await conn.execute("INSERT INTO schema_migrations(name) VALUES ('intent_rules') ON CONFLICT DO NOTHING")
            await conn.execute(damage)
            assert await migration.pending(conn)
            await migration.apply(conn, sql="", state="")
            assert not await migration.pending(conn)

    run_db(test)


def test_rule_versions_reorder_delete_and_rollback_are_append_only():
    from looper.db import intent_rules as store

    async def test(pool):
        first, second = str(uuid4()), str(uuid4())
        rule = {"id": first, "version": 1, "enabled": True, "position": 1, "intent_type": "any",
                "scope": {"kind": "all"}, "conditions": [], "action": "push_for_confirmation",
                "project_id": None, "template_key": None}
        async with pool.acquire() as conn, conn.transaction():
            await store.lock_admission(conn)
            assert (await store.lock_config(conn))["generation"] == 0
            await store.append_rule_version(conn, rule, edited_by="operator")
            await store.append_rule_version(conn, {**rule, "id": second, "position": 2}, edited_by="operator")
        async with pool.acquire() as conn, conn.transaction():
            await store.append_rule_version(conn, {**rule, "version": 2, "position": 2}, edited_by="operator")
            await store.append_rule_version(conn, {**rule, "id": second, "version": 2, "position": 1},
                                            edited_by="operator")
        async with pool.acquire() as conn, conn.transaction():
            await store.append_rule_version(conn, {**rule, "version": 3}, edited_by="operator",
                                            rolled_back_from=1)
            await store.append_rule_version(conn, {**rule, "id": second, "version": 3, "position": 2},
                                            edited_by="operator", deleted=True)
        current = await store.read_rules(pool)
        assert current == [{**rule, "version": 3}]
        history = await store.history(pool, first)
        assert [version["version"] for version in history["versions"]] == [1, 2, 3]
        assert history["versions"][-1]["rolled_back_from"] == 1
        assert (await store.history(pool, second))["current"]["deleted_at"] is not None
        # Even direct SQL cannot rewrite the historical contract.
        with pytest.raises(asyncpg.RaiseError, match="append-only"):
            await pool.execute("UPDATE intent_rule_versions SET enabled=false WHERE rule_id=$1", first)

    run_db(test)


def test_rule_hit_evidence_deletion_drops_the_whole_hit():
    from looper.db import intent_rules as store
    from tests import subscription_support as stream

    async def test(pool):
        await stream.source(pool)
        await stream.chat(pool, "oc_rule_synthetic")
        seqs = [await stream.add(pool, "synthetic evidence", chat_id="oc_rule_synthetic", sent_at=NOW)
                for _ in range(2)]
        hit = {"evaluation_key": "evaluation-one", "intent_id": None, "discovery_id": 1, "generation": 0,
               "rule_id": None, "rule_version": None, "selection": "fallback",
               "action": "push_for_confirmation", "reason": None, "verified_at": None, "mr_identity": None}
        async with pool.acquire() as conn, conn.transaction():
            hit_id = await store.append_hit(conn, hit, conditions=[], sources=seqs)
            assert await store.append_hit(conn, hit, conditions=[], sources=seqs) == hit_id
        assert len((await store.get_hit(pool, hit_id))["sources"]) == 2
        await pool.execute("DELETE FROM source_events WHERE seq=$1", seqs[0])
        assert await store.get_hit(pool, hit_id) is None
        assert await pool.fetchval("SELECT count(*) FROM intent_rule_hit_sources") == 0
        assert await pool.fetchval("SELECT count(*) FROM intent_rule_condition_results") == 0

    run_db(test)


def test_uninstalled_execution_counts_are_unavailable():
    from looper.db import intent_rules as store

    async def test(pool):
        stats = await store.hit_stats(pool, since=NOW, until=NOW.replace(day=21))
        assert stats["available"] is False
        assert stats["rules"] == []

    run_db(test)
