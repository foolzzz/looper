"""A self-contained patched-v2 fixture, only on disposable PostgreSQL.

This card checks the input corpus, not schema migration or launch parity.
The embedded schema is pinned independently of today's v3 initializer.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import uuid
from pathlib import Path

import asyncpg
import pytest

from looper.agents.registry_migration import plan
from looper.db import agent_sessions, llm_records, schedules
from looper.db.registry_migration import read_snapshot

DSN = os.environ.get("LOOPER_TEST_DATABASE_URL")
SEED = Path(__file__).parent / "fixtures" / "llm_registry_v2_seed.sql"
pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")


def _run(check):
    async def run():
        conn = await asyncpg.connect(DSN, command_timeout=15)
        schema = f"looper_seed_test_{uuid.uuid4().hex}"
        try:
            await conn.execute(f'CREATE SCHEMA "{schema}"')
            await conn.execute(f'SET search_path TO "{schema}"')
            await conn.execute(SEED.read_text(encoding="utf-8"))
            await check(conn)
        finally:
            await conn.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
            await conn.close()

    asyncio.run(asyncio.wait_for(run(), timeout=45))


async def _state(conn):
    tables = await conn.fetch("SELECT tablename FROM pg_tables WHERE schemaname = current_schema() ORDER BY 1")
    return {r["tablename"]: await conn.fetch(
        f'SELECT to_jsonb(t) AS row FROM "{r["tablename"]}" t ORDER BY to_jsonb(t)::text') for r in tables}


def test_seed_is_real_patched_v2_with_representative_nonempty_counts():
    async def check(conn):
        # Fail if this fixture ever starts using the current v3 initializer.
        columns = await conn.fetch("SELECT table_name, column_name FROM information_schema.columns "
                                   "WHERE table_schema = current_schema()")
        by_table = {}
        for row in columns:
            by_table.setdefault(row["table_name"], set()).add(row["column_name"])
        assert {"protocols", "base_url", "models", "env"} <= by_table["llm_providers"]
        assert {"provider_id", "model", "effort", "custom_prompt", "agent_teams"} <= by_table["agent_profiles"]
        assert not {"vendor_type", "model_id", "system_prompt_mode"} & by_table["agent_profiles"]
        assert "vendor_type" not in by_table["llm_providers"]
        assert not {"llm_models", "llm_provider_endpoints", "llm_model_migration_map", "registry_schema_version"} & by_table.keys()
        assert {"planner_session_provider", "planner_session_profile", "planner_session_task_id"} <= by_table["schedules"]
        assert "system_prompt_mode" in by_table["agent_setting_layers"]
        sql = SEED.read_text(encoding="utf-8")
        ddl = sql.split("-- BEGIN PINNED V2 SCHEMA\n")[1].split("-- END PINNED V2 SCHEMA")[0]
        assert hashlib.sha256(ddl.encode()).hexdigest() == "4ed948dbb67179337bae90829a19e2b205628665928abd028af0168a223a75dd"
        expected = {"agent_runtimes": 5, "llm_providers": 16, "agent_profiles": 20,
                    "projects": 4, "tasks": 24, "task_steps": 24, "agent_setting_layers": 21,
                    "task_profile_overrides": 72, "task_planner_profiles": 48, "task_roles": 24,
                    "project_role_templates": 4, "project_allowed_profiles": 80,
                    "schedules": 4, "schedule_profile_overrides": 12, "agent_sessions": 25,
                    "llm_records": 144}
        for table, count in expected.items():
            assert await conn.fetchval(f"SELECT count(*) FROM {table}") == count, table
        assert await conn.fetchval("SELECT count(*) FROM schema_migrations") > 10
        assert await conn.fetchval("SELECT count(*) FROM llm_providers WHERE builtin") == 3
        assert await conn.fetchval("SELECT count(*) FROM agent_profiles WHERE builtin") == 1
    _run(check)


def test_provider_catalog_protocol_key_and_conflict_matrix():
    async def check(conn):
        providers = {r["id"]: {**dict(r), "models": json.loads(r["models"]), "env": json.loads(r["env"])}
                     for r in await conn.fetch("SELECT * FROM llm_providers")}
        assert {p["oauth_runtime_type"] for p in providers.values() if p["auth"] == "oauth"} == {
            "claude-code", "codex", "grok"}
        custom = providers["prov-seed-custom"]
        assert custom["auth"] == "api_key" and custom["base_url"].endswith(".invalid/v1")
        assert custom["models"] == []
        default = providers["prov-seed-default"]
        assert default["base_url"] == "" and len(default["models"]) == 2
        assert providers["prov-seed-dual"]["protocols"] == ["anthropic-messages", "openai-responses"]
        assert providers["prov-seed-multi"]["protocols"] == ["openai-chat", "anthropic-messages", "openai-responses"]
        catalog = providers["prov-seed-multi"]["models"]
        assert [m["name"] for m in catalog] == ["seed-model[1m]", "seed-model", "seed-background"]
        assert catalog[0]["context_window"] == 1000000
        assert catalog[0]["background_model"] == "seed-background"
        assert catalog[0]["efforts"] == ["low", "high", "max"]
        assert catalog[1]["efforts"] == [] and catalog[2]["efforts"] is None
        assert catalog[0]["cache_write_usd_per_mtok"] == 0
        assert catalog[2]["input_usd_per_mtok"] is None
        keys = {r["provider_id"]: r["api_key"] for r in await conn.fetch("SELECT * FROM llm_provider_secrets")}
        assert keys and all(v.startswith("sk-synthetic-v2-seed-") for v in keys.values())
        assert keys["prov-seed-merge-a"] == keys["prov-seed-merge-b"]
        assert keys["prov-seed-different"] != keys["prov-seed-merge-a"]
        assert "prov-seed-missing-a" not in keys and "prov-seed-missing-b" not in keys
        assert not {p["id"] for p in providers.values() if p["auth"] == "oauth"} & keys.keys()
        base = providers["prov-seed-merge-a"]
        for field in ("protocols", "base_url", "models", "env", "max_concurrency"):
            assert providers["prov-seed-merge-b"][field] == base[field]
        for suffix, field in [("env", "env"), ("price", "models"), ("endpoint", "base_url"),
                              ("limit", "max_concurrency")]:
            conflict = providers[f"prov-seed-{suffix}"]
            assert conflict[field] != base[field]
            assert keys[conflict["id"]] == keys[base["id"]]
            for other in {"protocols", "base_url", "models", "env", "max_concurrency"} - {field}:
                assert conflict[other] == base[other], (suffix, other)
        assert providers["prov-seed-limit"]["max_concurrency"] == 3
        assert base["max_concurrency"] is None
        # Positive control: rows survive the raw legacy snapshot without JOIN loss.
        snapshot = await read_snapshot(conn)
        assert len(snapshot.providers) == 16 and len(snapshot.profiles) == 20
        assert len(snapshot.session_provider_ids) == 29  # 25 sessions plus four schedule holders
        assert plan(snapshot).applicable
    _run(check)


def test_all_reference_layers_schedule_holder_sessions_and_old_costs_are_readable():
    async def check(conn):
        assert {r["scope"] for r in await conn.fetch("SELECT DISTINCT scope FROM agent_setting_layers")} == {
            "system", "role", "project", "role_project"}
        for table in ("agent_setting_layers", "task_profile_overrides", "task_planner_profiles", "task_roles",
                      "project_allowed_profiles", "project_role_templates", "schedule_profile_overrides"):
            rows = await conn.fetch(f"SELECT r.profile_id, p.model FROM {table} r "
                                    "JOIN agent_profiles p ON p.id = r.profile_id")
            count = await conn.fetchval(f"SELECT count(*) FROM {table}")
            assert rows and len(rows) == count, table
            assert all(r["model"] for r in rows)
        assert await conn.fetchval("SELECT count(*) FROM task_planner_profiles WHERE step_id IS NULL") == 24
        assert await conn.fetchval("SELECT count(*) FROM task_planner_profiles WHERE step_id IS NOT NULL") == 24
        session = await agent_sessions.get_session("manager", pool=conn)
        assert session and session["session_id"] == "seed-session-manager"
        assert session["session_totals"]["cost_usd"] == 0.125
        schedule = await schedules.get_schedule("sched-seed-1", pool=conn)
        assert schedule and schedule["planner_session_provider"] == "prov-seed-merge-b"
        assert schedule["planner_session_profile"] == "agent-seed-merge-b"
        assert schedule["planner_session_task_id"] == "task-seed-1"
        assert schedule["planner_session_id"] == "seed-session-1"
        assert await conn.fetchval("SELECT planner_session_label FROM tasks WHERE id = 'task-seed-1'") == (
            schedules.planner_storage_label("sched-seed-1"))
        assert schedule["profile_overrides"] and schedule["task_limits"]
        holder = await agent_sessions.get_session("planner-task-seed-1", pool=conn)
        assert holder and holder["provider_id"] == schedule["planner_session_provider"]
        assert holder["session_id"] == schedule["planner_session_id"]
        assert holder["session_totals"] == json.loads(schedule["planner_session_totals"])
        costs = await llm_records.task_costs([f"task-seed-{i}" for i in range(1, 25)], pool=conn)
        assert len(costs) == 24
        for cost in costs.values():
            assert cost["runs"] == 5 and cost["cost_usd"] == pytest.approx(0.6)
            assert cost["running"] == 1 and cost["unpriced_runs"] == 1
            assert cost["equivalent_runs"] == 1 and cost["provider_price_runs"] == 2
            assert cost["manager_by_model"][0]["cost_usd"] == pytest.approx(0.05)
            assert {r["model"] for r in cost["by_model"]} == {"opus", "seed-model", "seed-unknown"}
            assert all(sum(r[k] for r in cost["by_model"]) > 0 for k in (
                "input_tokens", "output_tokens", "cache_read_tokens", "cache_creation_tokens"))
        spend = await llm_records.spend_since(None, pool=conn)
        assert spend["runs"] == 144
        assert spend["equivalent_usd"] == pytest.approx(24 * 0.1)
        assert spend["provider_priced_usd"] == pytest.approx(24 * 0.55)
    _run(check)


def test_seed_loads_twice_without_changing_any_rows_or_history():
    async def check(conn):
        before = await _state(conn)
        assert before["llm_records"] and before["schedules"]
        await conn.execute(SEED.read_text(encoding="utf-8"))
        assert await _state(conn) == before
    _run(check)


@pytest.mark.parametrize("column,target", [("runtime_id", "agent_runtimes"), ("provider_id", "llm_providers")])
def test_real_foreign_keys_reject_dangling_profile_references(column, target):
    async def check(conn):
        assert await conn.fetchval(f"SELECT count(*) FROM {target}") > 0
        before = await conn.fetchrow("SELECT * FROM agent_profiles WHERE id = 'agent-seed-custom'")
        assert before
        with pytest.raises(asyncpg.ForeignKeyViolationError):
            await conn.execute(f"UPDATE agent_profiles SET {column} = 'seed-does-not-exist' "
                               "WHERE id = 'agent-seed-custom'")
        assert await conn.fetchrow("SELECT * FROM agent_profiles WHERE id = 'agent-seed-custom'") == before
    _run(check)


def test_out_of_catalog_pairing_is_a_separate_blocking_sample():
    async def check(conn):
        assert plan(await read_snapshot(conn)).applicable
        # Legal v2 SQL but no longer a valid runtime/catalog pairing. It must
        # never contaminate the positive corpus or need a schema migration.
        await conn.execute("UPDATE agent_profiles SET model = 'seed-not-in-finite-catalog' "
                           "WHERE id = 'agent-seed-default'")
        result = plan(await read_snapshot(conn))
        assert not result.applicable
        assert result.report["unresolved_profiles"] > 0
        await conn.execute("UPDATE agent_profiles SET model = 'gpt-6.1-sol' WHERE id = 'agent-seed-default'")
        assert plan(await read_snapshot(conn)).applicable
    _run(check)


@pytest.mark.parametrize("column", ["runtime_id", "provider_id"])
def test_corrupt_foreign_key_is_a_separate_blocking_sample(column):
    async def check(conn):
        assert plan(await read_snapshot(conn)).applicable
        # Simulate a damaged legacy dump only in this disposable schema.
        # Keep the positive fixture's actual constraints intact everywhere else.
        constraint = await conn.fetchval(
            "SELECT conname FROM pg_constraint c JOIN pg_attribute a "
            "ON a.attrelid = c.conrelid AND a.attnum = ANY(c.conkey) "
            "WHERE c.conrelid = 'agent_profiles'::regclass AND c.contype = 'f' AND a.attname = $1", column)
        assert constraint
        await conn.execute(f'ALTER TABLE agent_profiles DROP CONSTRAINT "{constraint}"')
        await conn.execute(f"UPDATE agent_profiles SET {column} = 'seed-does-not-exist' "
                           "WHERE id = 'agent-seed-custom'")
        snapshot = await read_snapshot(conn)
        assert len(snapshot.profiles) == 20  # raw read must not hide the dangling row
        result = plan(snapshot)
        assert not result.applicable and result.report["unresolved_profiles"] > 0
    _run(check)
