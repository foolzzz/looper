"""Only authentic bridge databases cross the schema upgrade boundary."""
import asyncio
import json

import pytest

from looper.db import bridge_upgrade, connection
from tests.bridge_schema_support import (
    bridge_source as bridge_source, snapshot_bridge_source as snapshot_bridge_source,
    initialize_bridge, run_bridge, structure, witness,
)
from tests.db_support import DSN, fresh_connection, fresh_schema

pytestmark = pytest.mark.skipif(not DSN, reason="requires disposable LOOPER_TEST_DATABASE_URL")


def run(test):
    asyncio.run(asyncio.wait_for(test(), 90))


def test_bridge_normalization_preserves_evidence_and_matches_fresh(bridge_source):
    async def test():
        async with fresh_connection() as conn, fresh_connection() as fresh:
            await initialize_bridge(conn, bridge_source)
            before = await witness(conn)
            shape = await structure(conn)
            assert await connection.schema_change_pending(conn)
            assert await witness(conn) == before and await structure(conn) == shape
            await connection.ensure_schema(conn)
            after = await witness(conn)
            for table in ("task_plans", "system_settings", "library_items", "library_files", "library_installs",
                          "chat_messages", "llm_model_migration_map", "agent_setting_layers"):
                assert after[table] == before[table], table
            assert json.loads(after["tasks"][0]["config"]) == {"worker": {"max_turns": 9}}
            assert {r["id"]: r["task_workspaces"] for r in after["projects"]} == {
                "proj-bridge": "in_place", "proj-explicit": "worktree"}
            sessions = {r["agent_id"]: r for r in after["agent_sessions"]}
            original = {r["agent_id"]: r for r in before["agent_sessions"]}
            assert sessions["manager-bridge"]["runtime_type"] == "claude-code"
            assert sessions["manager-bridge"]["provider_id"] == "prov-claude-subscription"
            assert sessions["manager-bridge"]["session_totals"] == original["manager-bridge"]["session_totals"]
            assert sessions["manager-bridge"]["pending_handoff"] == "Keep the handoff"
            for key in ("manager-empty", "manager-codex", "manager-provider"):
                assert sessions[key] == original[key]
            for row in after["channel_configs"]:
                assert json.loads(row["config"])["allowed_user_ids"] == ["7", "8"]
            metadata = json.loads(after["llm_records"][0]["metadata"])
            assert metadata["agent"] == metadata["profile"] == {"name": "Original evidence"}
            assert metadata["accounting_basis"] == "subscription_equivalent"
            assert after["task_user_gate_messages"] == before["task_user_gate_messages"]
            assert after["task_user_gate_posts"][0] == {**before["task_user_gate_posts"][0], "channel": "telegram"}
            assert {r["name"] for r in before["schema_migrations"]} <= {r["name"] for r in after["schema_migrations"]}
            assert await conn.fetchval("SELECT planner_session_runtime FROM schedules") == "claude-code"
            # The bridge predates execution snapshots; adding the column preserves every historical run field.
            runs = [dict(row) for row in after["schedule_runs"]]
            assert len(runs) == 1
            assert all(row.pop("execution_snapshot") is None for row in runs)
            assert runs == before["schedule_runs"]
            await connection.ensure_schema(fresh)
            assert await structure(conn) == await structure(fresh)
            await connection.ensure_schema(conn)
            assert await witness(conn) == after
            assert not await connection.schema_change_pending(conn)
            await run_bridge(bridge_source, DSN, await conn.fetchval("SELECT current_schema()"), refuse=True)
            assert await witness(conn) == after
    run(test)


@pytest.mark.parametrize("replay_old_bridge", [False, True])
def test_released_bridge_preserves_execution_snapshot(snapshot_bridge_source, bridge_source, replay_old_bridge):
    async def test():
        async with fresh_connection() as conn, fresh_connection() as fresh:
            await initialize_bridge(conn, snapshot_bridge_source)
            snapshot = {"requirement": "Claimed before the schedule edit", "config": {"max_turns": 9}}
            await conn.execute("UPDATE schedule_runs SET execution_snapshot=$1::jsonb", json.dumps(snapshot))
            if replay_old_bridge:
                # The older release rewrites the fingerprint but keeps the newer column and its data.
                await run_bridge(bridge_source, DSN, await conn.fetchval("SELECT current_schema()"))
            before, shape = await witness(conn), await structure(conn)
            assert await conn.fetchval("SELECT version FROM registry_schema_version") == 3
            assert await connection.schema_change_pending(conn)
            assert await witness(conn) == before and await structure(conn) == shape
            await connection.ensure_schema(conn)
            after = await witness(conn)
            assert after["schedule_runs"] == before["schedule_runs"]
            assert json.loads(after["schedule_runs"][0]["execution_snapshot"]) == snapshot
            assert await conn.fetchval("SELECT version FROM registry_schema_version") == 4
            await connection.ensure_schema(fresh)
            assert await structure(conn) == await structure(fresh)
            await connection.ensure_schema(conn)
            assert await witness(conn) == after
            assert not await connection.schema_change_pending(conn)
    run(test)


@pytest.mark.parametrize("damage", [
    "ALTER TABLE schedule_runs DROP COLUMN execution_snapshot",
    "ALTER TABLE schedule_runs ALTER COLUMN execution_snapshot TYPE text USING execution_snapshot::text",
])
def test_incomplete_released_bridge_is_refused_without_mutation(snapshot_bridge_source, damage):
    async def test():
        async with fresh_connection() as conn:
            await initialize_bridge(conn, snapshot_bridge_source)
            await conn.execute(damage)
            before, shape = await witness(conn), await structure(conn)
            for operation in (connection.schema_change_pending, connection.ensure_schema):
                with pytest.raises(connection.RegistrySchemaVersionError):
                    await operation(conn)
                assert await witness(conn) == before and await structure(conn) == shape
    run(test)


def test_deleted_custom_bot_preserves_gate_posts_and_reply_identity(bridge_source):
    from looper.db import user_gates

    async def test():
        async with fresh_connection() as conn:
            await initialize_bridge(conn, bridge_source)
            await conn.execute("DELETE FROM channel_configs WHERE channel_id='channel-telegram-201'")
            before = await witness(conn)
            assert before["task_user_gate_posts"][0]["channel"] is None
            assert before["task_user_gate_posts"][0]["channel_id"] == "channel-telegram-201"
            assert await connection.schema_change_pending(conn)
            await connection.ensure_schema(conn)
            after = await witness(conn)
            assert after["task_user_gate_posts"] == before["task_user_gate_posts"]
            assert after["task_user_gate_messages"] == before["task_user_gate_messages"]
            gate = await user_gates.find_reference("telegram", "7", ["11"],
                                                  channel_id="channel-telegram-201", pool=conn)
            assert gate is not None and gate["step_id"] == "step-bridge"
            assert await user_gates.find_reference("telegram", "7", ["11"],
                                                   channel_id="channel-telegram", pool=conn) is None
            await connection.ensure_schema(conn)
            assert await witness(conn) == after
            assert not await connection.schema_change_pending(conn)
    run(test)


@pytest.mark.parametrize("current,damage,match", [(False, damage, "v2.13.165") for damage in [
    "UPDATE registry_schema_version SET version=2",
    "UPDATE schema_fingerprint SET sha256='unknown'",
    "DELETE FROM registry_schema_version",
    "DROP TABLE registry_schema_version",
    "DELETE FROM schema_fingerprint",
    "DROP TABLE schema_fingerprint",
    "DELETE FROM schema_migrations WHERE name='llm_model_registry_v3'",
    "ALTER TABLE agent_sessions DROP COLUMN selected_profile_id",
    "ALTER TABLE channel_configs DISABLE TRIGGER channel_configs_manager_fixed",
    "ALTER TABLE agent_sessions ALTER COLUMN updated_at TYPE timestamptz(0)",
    "ALTER TABLE agent_sessions DROP COLUMN session_totals",
    "ALTER TABLE schedules DROP CONSTRAINT schedules_notify_check",
]] + [
    (True, "UPDATE registry_schema_version SET version=5", "exceeds supported"),
    (True, "DELETE FROM schema_migrations WHERE name='post_bridge_baseline'", "v2.13.165"),
    (True, "UPDATE registry_schema_version SET version=3", "v2.13.165"),
])
def test_partial_or_unsupported_identity_is_refused_without_mutation(bridge_source, current, damage, match):
    async def test():
        async with fresh_connection() as conn:
            if current:
                await connection.ensure_schema(conn)
            else:
                await initialize_bridge(conn, bridge_source)
            await conn.execute(damage)
            before, shape = await witness(conn), await structure(conn)
            for operation in (connection.schema_change_pending, connection.ensure_schema):
                with pytest.raises(connection.RegistrySchemaVersionError, match=match):
                    await operation(conn)
                assert await witness(conn) == before and await structure(conn) == shape
    run(test)


@pytest.mark.parametrize("damage,match", [
    (None, "injected failure"), ("UPDATE tasks SET config='[]'", "bridge task task-bridge"),
    ("UPDATE llm_records SET metadata='[]'", "metadata must be an object"),
    ("UPDATE channel_configs SET config='{\"allowed_user_ids\":[true]}'", "bridge channel"),
    ("UPDATE task_user_gate_posts SET channel_id=NULL", "bot identity cannot be recovered"),
])
def test_failed_upgrade_rolls_back_all_rows_and_constraints(bridge_source, monkeypatch, damage, match):
    original = bridge_upgrade.upgrade_from_bridge

    async def fail(conn):
        await original(conn)
        raise ValueError("injected failure after normalization")

    async def test():
        async with fresh_connection() as conn:
            await initialize_bridge(conn, bridge_source)
            if damage:
                await conn.execute(damage)
            before, shape = await witness(conn), await structure(conn)
            if damage is None:
                monkeypatch.setattr(bridge_upgrade, "upgrade_from_bridge", fail)
            with pytest.raises(ValueError, match=match):
                await connection.ensure_schema(conn)
            assert await witness(conn) == before and await structure(conn) == shape
    run(test)


def test_concurrent_bridge_starts_are_serialized_and_second_start_is_invariant(bridge_source):
    async def test():
        async with fresh_schema() as pool:
            await initialize_bridge(pool, bridge_source)
            await asyncio.gather(connection.ensure_schema(pool), connection.ensure_schema(pool))
            before, shape = await witness(pool), await structure(pool)
            await asyncio.gather(connection.ensure_schema(pool), connection.ensure_schema(pool))
            assert await witness(pool) == before and await structure(pool) == shape
    run(test)


def test_visible_installation_cannot_be_shadowed_by_an_empty_schema():
    async def test():
        async with fresh_connection() as conn:
            await connection.ensure_schema(conn)
            owner = await conn.fetchval("SELECT current_schema()")
            await conn.execute('CREATE SCHEMA shadow_gate; SET search_path TO shadow_gate, "' + owner + '"')
            try:
                with pytest.raises(connection.RegistrySchemaVersionError, match="namespaces"):
                    await connection.ensure_schema(conn)
                assert not await conn.fetchval("SELECT EXISTS (SELECT 1 FROM pg_class WHERE relnamespace='shadow_gate'::regnamespace)")
            finally:
                await conn.execute('SET search_path TO "' + owner + '"; DROP SCHEMA shadow_gate')
    run(test)


def test_current_baseline_does_not_replay_pruned_history():
    async def test():
        async with fresh_connection() as conn:
            await connection.ensure_schema(conn)
            await conn.execute("DELETE FROM schema_migrations WHERE name <> 'post_bridge_baseline'; "
                               "UPDATE agent_profiles SET agent_teams=false")
            before = await witness(conn)
            await connection.ensure_schema(conn)
            assert await witness(conn) == before
            assert not await conn.fetchval("SELECT bool_or(agent_teams) FROM agent_profiles")
    run(test)
