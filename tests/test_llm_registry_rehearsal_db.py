"""Rehearse one patched-v2 snapshot on disposable source and migration DBs.

The source is seeded once with the independently pinned fixture. PostgreSQL
clones it before preflight, so migration and restart must preserve that exact
snapshot, including timestamps, profile references and historical usage.
"""
from __future__ import annotations

import asyncio
import json
import uuid
from contextlib import asynccontextmanager
from dataclasses import replace
from urllib.parse import urlsplit, urlunsplit

import asyncpg
import pytest

from looper.agents.profile_registry import ModelSpec, ResolvedAgentProfile
from looper.agents.registry_migration import MigrationBlocked, _launch_inputs, plan
from looper.agents.run_usage import Usage
from looper.agents.runtimes import ADAPTERS, AuthKind
from looper.agents.runtimes.pricing import pricer
from looper.db import connection, llm_records, profile_registry, registry_migration
from tests.db_support import DSN
from tests.test_llm_registry_v2_seed_db import SEED
from tests.test_registry_migration import REPORT_FIELDS
from tests.test_registry_migration_report_db import state

pytestmark = pytest.mark.skipif(not DSN, reason="requires a disposable LOOPER_TEST_DATABASE_URL")
USAGE = Usage(input_tokens=1000, output_tokens=100, cache_read_tokens=200, cache_creation_tokens=50)


def _dsn(database):
    parts = urlsplit(DSN)
    return urlunsplit(parts._replace(path=f"/{database}"))


@asynccontextmanager
async def _snapshot_databases():
    # Never initialize the database named by DSN: it is only the disposable
    # administration connection, matching the other PostgreSQL test suites.
    admin = await asyncpg.connect(DSN, command_timeout=15)
    prefix = f"looper_rehearsal_{uuid.uuid4().hex}"
    source_name, target_name = f"{prefix}_source", f"{prefix}_target"
    source = target = None
    try:
        await admin.execute(f'CREATE DATABASE "{source_name}"')
        source = await asyncpg.connect(_dsn(source_name), command_timeout=15)
        await source.execute(SEED.read_text(encoding="utf-8"))
        await source.close()
        source = None
        await admin.execute(f'CREATE DATABASE "{target_name}" TEMPLATE "{source_name}"')
        source = await asyncpg.connect(_dsn(source_name), command_timeout=15)
        target = await asyncpg.connect(_dsn(target_name), command_timeout=15)
        yield source, target, _dsn(target_name)
    finally:
        for conn in (target, source):
            if conn is not None:
                await conn.close()
        try:
            for name in (target_name, source_name):
                await admin.execute(f'DROP DATABASE IF EXISTS "{name}"')
        finally:
            await admin.close()


async def _history(conn):
    tasks = [row["id"] for row in await conn.fetch("SELECT id FROM tasks ORDER BY id")]
    return (await llm_records.task_costs(tasks, pool=conn),
            await llm_records.spend_since(None, pool=conn))


def _assert_report(report, *, blocked=False):
    assert report.keys() == REPORT_FIELDS
    for key, value in report.items():
        values = value.values() if key == "providers_kept_separate_by_reason" else [value]
        assert all(type(n) is int and n >= 0 for n in values)
    assert report["providers_before"] == 16
    assert report["profiles_before"] == 20
    if blocked:
        assert report["unresolved_profiles"] == 1
        assert report["profiles_mapped"] == report["profiles_after"] == 19
    else:
        assert report["profiles_mapped"] == report["profiles_after"] == 20
        assert all(report[k] == 0 for k in (
            "unresolved_profiles", "binding_mismatches", "pricing_mismatches"))


async def _assert_readback(conn, old, conversion, before, history):
    report = plan(old).report
    for table, field in (("llm_providers", "providers_after"),
                         ("llm_provider_endpoints", "endpoints_after"),
                         ("agent_profiles", "profiles_after")):
        assert await conn.fetchval(f"SELECT count(*) FROM {table}") == report[field], table
    # Dry-run counts converted legacy models. Normal startup additionally
    # fills the shipped OAuth catalogs: exactly eleven unreferenced builtins
    # for this fixture, without replacing any converted identity or setting.
    persisted = {r["id"]: dict(r) for r in await conn.fetch("SELECT * FROM llm_models")}
    assert len(conversion.models) == report["models_after"] == 27
    assert len(persisted) == 38
    assert conversion.models.keys() <= persisted.keys()
    additions = persisted.keys() - conversion.models.keys()
    assert len(additions) == 11
    assert all(persisted[key]["builtin"] for key in additions)
    assert {persisted[key]["provider_id"] for key in additions} <= {
        "prov-claude-subscription", "prov-codex-login", "prov-grok-login"}
    mapped = {tuple(row) for row in await conn.fetch(
        "SELECT legacy_provider_id, legacy_model, protocol, provider_id, model_id FROM llm_model_migration_map")}
    assert mapped == {(*key, *value) for key, value in conversion.model_map.items()}
    after = await state(conn)
    # Compare every pre-existing table, not just the joined reference counts.
    changed = {"agent_profiles", "llm_providers", "llm_provider_secrets", "schema_migrations",
               "agent_sessions", "schedules", "agent_setting_layers", "projects", "tasks"}
    for table, rows in before[1].items():
        if table not in changed:
            assert after[1][table] == rows, table
    # The seed predates the projects' wait commands: the schema adds that column, NULL, and nothing else changes.
    projects = [json.loads(r[0]) for r in after[1]["projects"]]
    assert all(row.pop("wait_commands") is None for row in projects)
    assert projects == [json.loads(r[0]) for r in before[1]["projects"]]
    # The seed predates the Managers' tasks: every old task is the default Manager's, without a template or a thread.
    tasks = [json.loads(r[0]) for r in after[1]["tasks"]]
    assert all((row.pop("manager_id"), row.pop("template"), row.pop("origin_thread")) == ("manager", None, None)
               for row in tasks)
    assert tasks == [json.loads(r[0]) for r in before[1]["tasks"]]
    layers = [json.loads(r[0]) for r in before[1]["agent_setting_layers"]]
    # The already-completed effort migration left this obsolete column NULL;
    # contract removes it while keeping every reference and role prompt mode.
    assert all(row.pop("effort") is None for row in layers)
    assert sorted((json.loads(r[0]) for r in after[1]["agent_setting_layers"]), key=str) == sorted(layers, key=str)
    for table, column in (("agent_sessions", "provider_id"), ("schedules", "planner_session_provider")):
        expected = []
        for record in before[1][table]:
            row = json.loads(record[0])
            row[column] = conversion.provider_map.get(row[column], row[column])
            if table == "agent_sessions":  # the schema adds the Manager's selected profile, NULL for old rows
                row["selected_profile_id"] = None
            expected.append(row)
        assert sorted((json.loads(r[0]) for r in after[1][table]), key=str) == sorted(expected, key=str)
    profiles = await profile_registry.list_profiles(pool=conn)
    assert {p["id"] for p in profiles} == {p["id"] for p in old.profiles}
    models = [ModelSpec.from_entry({k: row[k] for k in ModelSpec.__dataclass_fields__})
              for row in await profile_registry.list_models(pool=conn)]
    rows = {row["id"]: row for row in old.profiles}
    originals = {row["id"]: row for r in before[1]["agent_profiles"] if (row := json.loads(r[0]))}
    for row in profiles:
        identity = row["id"]
        profile = ResolvedAgentProfile.from_row(row)
        account = replace(profile.resolved_model.provider,
                          api_key=await profile_registry.get_api_key(row["provider_id"], pool=conn))
        resolved = replace(profile.resolved_model, provider=account)
        assert profile.model_id == conversion.profiles[identity].model_id
        saved = json.loads(await conn.fetchval("SELECT to_jsonb(a) FROM agent_profiles a WHERE id = $1", identity))
        for field in ("name", "runtime_id", "effort", "agent_teams", "custom_prompt", "builtin",
                      "created_at", "updated_at"):
            # Compare raw SQL values (timestamps included) rather than serialized API values.
            assert saved[field] == originals[identity][field], field
        target = resolved.binding_provider()
        url = resolved.endpoint.base_url or (old.rules.default_urls.get((profile.runtime.type, resolved.model.protocol))
                                             if account.auth is AuthKind.API_KEY else None)
        # Reuse the static launch capture against actual SQL readback, never
        # against the in-memory converter output used by dry-run.
        launch = _launch_inputs(profile.runtime, target, {**dict(rows[identity]), "model": profile.model,
                                "effort": profile.effort, "agent_teams": profile.agent_teams,
                                "custom_prompt": profile.custom_prompt}, resolved.model.protocol, url,
                                resolved=resolved)
        assert launch == old.bindings[identity]
        assert ADAPTERS[profile.runtime.type].cost_source(target) == (
            ADAPTERS[profile.runtime.type].cost_source(old.pricing[identity]))
        previous, current = pricer(old.pricing[identity]), pricer(resolved, catalog=models)
        probes = {profile.model, "seed-unknown", "seed-model-20261020", "SEED-MODEL[1m]"}
        probes.update(m.name for m in old.pricing[identity].catalog)
        for name in probes:
            assert current.model_price(name) == previous.model_price(name)
            assert current(name, USAGE) == previous(name, USAGE)
    assert await _history(conn) == history


def test_same_snapshot_dry_run_migration_and_second_start(record_property):
    async def run():
        async with _snapshot_databases() as (source, target, target_dsn):
            before = await state(source)
            assert await state(target) == before
            assert await source.fetchval("SELECT to_regclass('llm_models')") is None
            old = await registry_migration.read_snapshot(source)
            result = plan(old)
            assert result.applicable
            report = await registry_migration.dry_run(target)
            assert report == result.report
            _assert_report(report)
            assert await state(target) == before
            history = await _history(source)
            costs, spend = history
            assert len(costs) == 24 and spend["runs"] == 144
            assert spend["equivalent_usd"] == pytest.approx(2.4)
            assert spend["provider_priced_usd"] == pytest.approx(13.2)
            assert all(c["runs"] == 5 and c["unpriced_runs"] == 1 and c["running"] == 1
                       and c["cost_usd"] == pytest.approx(0.6) for c in costs.values())
            assert await connection.schema_change_pending(target)
            await connection.ensure_schema(target)
            assert await target.fetchval("SELECT version FROM registry_schema_version") == 3
            await _assert_readback(target, old, result.conversion, before, history)
            migrated = await state(target)
            # A new connection exercises startup without the first one's cache.
            restart = await asyncpg.connect(target_dsn, command_timeout=15)
            try:
                assert not await connection.schema_change_pending(restart)
                await connection.ensure_schema(restart)
                assert await state(restart) == migrated
                await _assert_readback(restart, old, result.conversion, before, history)
            finally:
                await restart.close()
            assert await state(source) == before
            record_property("migration_report", json.dumps(report, sort_keys=True))

    asyncio.run(asyncio.wait_for(run(), timeout=90))


@pytest.mark.parametrize("failure", ["runtime_id", "provider_id", "model"])
def test_bad_reference_blocks_without_repair_or_partial_migration(failure, record_property):
    async def run():
        async with _snapshot_databases() as (source, target, _):
            assert plan(await registry_migration.read_snapshot(source)).applicable
            if failure != "model":
                constraint = await target.fetchval(
                    "SELECT conname FROM pg_constraint c JOIN pg_attribute a "
                    "ON a.attrelid = c.conrelid AND a.attnum = ANY(c.conkey) "
                    "WHERE c.conrelid = 'agent_profiles'::regclass AND c.contype = 'f' AND a.attname = $1",
                    failure)
                assert constraint
                await target.execute(f'ALTER TABLE agent_profiles DROP CONSTRAINT "{constraint}"')
            await target.execute(f"UPDATE agent_profiles SET {failure} = 'seed-invalid-reference' "
                                 "WHERE id = 'agent-seed-default'")
            before = await state(target)
            report = await registry_migration.dry_run(target)
            _assert_report(report, blocked=True)
            assert await state(target) == before
            with pytest.raises(MigrationBlocked):
                await connection.ensure_schema(target)
            assert await state(target) == before
            assert await target.fetchval("SELECT to_regclass('registry_schema_version')") is None
            assert await target.fetchval(f"SELECT {failure} FROM agent_profiles WHERE id = 'agent-seed-default'") == (
                "seed-invalid-reference")
            record_property("migration_report", json.dumps(report, sort_keys=True))

    asyncio.run(asyncio.wait_for(run(), timeout=90))
