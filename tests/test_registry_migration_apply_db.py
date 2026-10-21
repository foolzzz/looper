"""Atomic legacy registry application against disposable PostgreSQL only."""

import asyncio
import json
import os
from pathlib import Path

import asyncpg
import pytest

from looper.agents.registry_migration import MigrationBlocked, plan
from looper.db import connection, registry_migration, schedules
from tests.test_llm_model_schema_db import _database
from tests.test_registry_migration_report_db import state

DSN = os.environ.get('LOOPER_TEST_DATABASE_URL')
pytestmark = pytest.mark.skipif(not DSN, reason='LOOPER_TEST_DATABASE_URL is not set')


async def _legacy(conn):
    await conn.execute((Path(__file__).parent / "fixtures/schema_v2_6_0.sql").read_text())
    phases = connection._schema_phases(connection.SCHEMA_PATH.read_text())
    await conn.execute(phases["expand"])
    await conn.execute(phases["legacy"])
    await conn.execute("""
        INSERT INTO registry_schema_version VALUES (TRUE, 2);
        INSERT INTO agent_runtimes (id, name, type) VALUES ('rt-omp', 'omp', 'omp');
        INSERT INTO llm_providers (id, name, auth, protocols, base_url, models, vendor_type, created_at)
        VALUES ('prov-a', 'account-a', 'api_key', '{openai-responses}', 'https://example.invalid/v1',
                '[{"name":"Exact.Model[1m]","input_usd_per_mtok":0,"output_usd_per_mtok":2}]',
                'deepseek', '2026-01-01'),
               ('prov-b', 'account-b', 'api_key', '{openai-responses}', 'https://example.invalid/v1',
                '[{"name":"Exact.Model[1m]","input_usd_per_mtok":0,"output_usd_per_mtok":2}]',
                'deepseek', '2026-01-02');
        INSERT INTO llm_provider_secrets VALUES ('prov-a', 'test-only-key'), ('prov-b', 'test-only-key');
        INSERT INTO agent_profiles (id, name, runtime_id, provider_id, model, effort, custom_prompt)
        VALUES ('agent-test', 'test-profile', 'rt-omp', 'prov-b', 'Exact.Model[1m]', 'high', 'Keep this.');
        INSERT INTO projects (id, name) VALUES ('proj-test', 'test');
        INSERT INTO schedules (id, project_id, name, cron, timezone, requirement)
        VALUES ('sched-test', 'proj-test', 'test', '* * * * *', 'UTC', 'test');
        INSERT INTO tasks (id, project_id, requirement, schedule_id, status)
        VALUES ('task-test', 'proj-test', 'test', 'sched-test', 'completed');
        INSERT INTO agent_setting_layers (scope, role, profile_id, system_prompt_mode)
        VALUES ('role', 'worker', 'agent-test', 'append');
        INSERT INTO task_profile_overrides VALUES ('task-test', 'worker', 'agent-test');
        INSERT INTO task_system_prompt_modes VALUES ('task-test', 'worker', 'replace');
        INSERT INTO schedule_profile_overrides VALUES ('sched-test', 'worker', 'agent-test');
        INSERT INTO agent_sessions (agent_id, session_id, session_totals, runtime_type, provider_id, profile_id)
        VALUES ('planner-task-test', 'session-test', '{"cost_usd":1.25}', 'omp', 'prov-b', 'agent-test');
        UPDATE schedules SET planner_session_id = 'session-test', planner_session_provider = 'prov-b',
            planner_session_runtime = 'omp', planner_session_profile = 'agent-test',
            planner_session_task_id = 'task-test', planner_session_totals = '{"cost_usd":1.25}',
            planner_session_since = '2026-01-01';
        INSERT INTO llm_records (agent_id, model, metadata, cost_usd)
        VALUES ('planner-task-test', 'Exact.Model[1m]', '{"profile":{"provider":{"id":"prov-b"}}}', 1.25);
    """)


def test_apply_maps_once_and_preserves_sessions_modes_and_history(monkeypatch):
    async def run():
        async with _database() as conn:
            await _legacy(conn)
            old = await registry_migration._read_snapshot(conn)
            expected = plan(old)
            assert expected.applicable and expected.report["providers_merged"] == 1
            records = await conn.fetch("SELECT * FROM llm_records")
            session = dict(await conn.fetchrow("SELECT * FROM agent_sessions"))
            holder = dict(await conn.fetchrow("SELECT * FROM schedules"))
            assert await connection.schema_change_pending(conn)
            await connection.ensure_schema(conn)
            assert await conn.fetchval("SELECT version FROM registry_schema_version") == 3
            assert await conn.fetchval("SELECT 1 FROM schema_migrations WHERE name = 'llm_model_registry_v3'")
            assert not await conn.fetchval("SELECT EXISTS (SELECT 1 FROM llm_providers WHERE id = 'prov-b')")
            assert await conn.fetchval("SELECT count(*) FROM llm_provider_secrets WHERE provider_id = 'prov-b'") == 0
            assert dict(await conn.fetchrow("SELECT * FROM agent_sessions")) == {**session, "provider_id": "prov-a"}
            assert dict(await conn.fetchrow("SELECT * FROM schedules")) == {**holder, "planner_session_provider": "prov-a"}
            profile = await conn.fetchrow("SELECT * FROM agent_profiles WHERE id = 'agent-test'")
            assert (profile['effort'], profile['custom_prompt']) == ('high', 'Keep this.')
            assert profile['model_id'] == expected.conversion.profiles['agent-test'].model_id
            assert tuple(await conn.fetchrow("SELECT profile_id, system_prompt_mode FROM agent_setting_layers "
                                            "WHERE role = 'worker'")) == ('agent-test', 'append')
            assert await conn.fetchval("SELECT system_prompt_mode FROM task_system_prompt_modes") == 'replace'
            assert await conn.fetchval("SELECT profile_id FROM task_profile_overrides") == 'agent-test'
            assert await conn.fetchval("SELECT profile_id FROM schedule_profile_overrides") == 'agent-test'
            assert await conn.fetch("SELECT * FROM llm_records") == records
            mapped = await conn.fetch("SELECT * FROM llm_model_migration_map")
            assert mapped and set(mapped[0].keys()) == {
                'legacy_provider_id', 'legacy_model', 'protocol', 'provider_id', 'model_id'}
            assert not await conn.fetchval("SELECT EXISTS (SELECT 1 FROM pg_constraint WHERE "
                                          "conrelid = 'llm_model_migration_map'::regclass AND contype = 'f')")
            before = await state(conn)
            await connection.ensure_schema(conn)
            assert await state(conn) == before
            # Exercise the actual claim/write-through path after canonical identity changes.
            pool = await asyncpg.create_pool(DSN,
                                            min_size=1, max_size=2,
                                            server_settings={'search_path': await conn.fetchval('SELECT current_schema()')})
            try:
                monkeypatch.setattr(connection, '_pool', pool)
                claimed = await schedules.claim_planner_session('task-test')
                assert claimed['provider_id'] == 'prov-a'
                assert claimed['session_id'] == 'session-test'
                assert claimed['session_totals'] == {'cost_usd': 1.25}
                await schedules.save_planner_session('task-test', 'session-next', {'cost_usd': 2},
                                                     runtime_type='omp', provider_id='prov-a', profile_id='agent-test')
                assert await conn.fetchval("SELECT planner_session_provider FROM schedules") == 'prov-a'
                assert json.loads(await conn.fetchval("SELECT planner_session_totals FROM schedules")) == {'cost_usd': 2}
            finally:
                await pool.close()

    asyncio.run(asyncio.wait_for(run(), 45))


@pytest.mark.parametrize('failure', ['unknown_pair', 'binding_mismatches', 'pricing_mismatches', 'builtin'])
def test_blocked_or_late_failure_rolls_back_every_schema_and_data_change(failure, monkeypatch, tmp_path):
    async def run():
        async with _database() as conn:
            await _legacy(conn)
            if failure == 'unknown_pair':
                await conn.execute("UPDATE agent_profiles SET model = 'outside-catalog' WHERE id = 'agent-test'")
            elif failure == 'builtin':
                path = tmp_path / 'schema.sql'
                path.write_text(connection.SCHEMA_PATH.read_text() + '\nSELECT 1/0;\n')
                monkeypatch.setattr(connection, 'SCHEMA_PATH', path)
            else:
                def mismatch(old):
                    result = plan(old)
                    result.report[failure] = 1
                    return result
                monkeypatch.setattr(registry_migration, 'plan', mismatch)
            before = await state(conn)
            with pytest.raises((MigrationBlocked, connection.RegistrySchemaVersionError, asyncpg.DivisionByZeroError)):
                await connection.ensure_schema(conn)
            assert await state(conn) == before
            assert await conn.fetchval('SELECT version FROM registry_schema_version') == 2
            assert not await conn.fetchval("SELECT 1 FROM schema_migrations WHERE name = 'llm_model_registry_v3'")

    asyncio.run(asyncio.wait_for(run(), 45))


def test_legacy_and_fresh_registry_have_the_same_final_structure():
    from tests.test_profile_rename_migration_db import _structure

    async def run():
        async with _database() as old, _database() as fresh:
            await _legacy(old)
            await connection.ensure_schema(old)
            await connection.ensure_schema(fresh)
            assert await _structure(old) == await _structure(fresh)

    asyncio.run(asyncio.wait_for(run(), 45))


def test_unknown_legacy_pair_rolls_back_expansion_and_historical_migrations():
    async def run():
        async with _database() as conn:
            await conn.execute((Path(__file__).parent / 'fixtures/schema_v2_6_0.sql').read_text())
            await conn.execute("""
                INSERT INTO llm_providers (id, name, auth, protocols, base_url, models)
                VALUES ('prov-invalid', 'invalid-pair', 'api_key', '{openai-responses}',
                        'https://example.invalid', '[]');
                INSERT INTO agents (id, name, runtime_id, provider_id, model)
                VALUES ('agent-invalid', 'invalid-pair', 'rt-claude-code', 'prov-invalid', 'model');
            """)
            before = await state(conn)
            with pytest.raises(MigrationBlocked):
                await connection.ensure_schema(conn)
            assert await state(conn) == before
            for table in ('llm_models', 'llm_provider_endpoints', 'registry_schema_version', 'schema_fingerprint'):
                assert await conn.fetchval('SELECT to_regclass($1)', table) is None

    asyncio.run(asyncio.wait_for(run(), 45))


def test_apply_preserves_detected_pi_and_dsh_without_profiles():
    async def run():
        async with _database() as conn:
            await _legacy(conn)
            await conn.execute("INSERT INTO agent_runtimes (id, name, type) VALUES "
                               "('rt-pi', 'pi', 'pi'), ('rt-dsh', 'dsh', 'dsh')")
            profiles_before = await conn.fetchval("SELECT count(*) FROM agent_profiles")
            expected = plan(await registry_migration._read_snapshot(conn))
            assert expected.applicable
            assert expected.report["profiles_mapped"] == profiles_before > 0
            await connection.ensure_schema(conn)
            assert await conn.fetchval("SELECT version FROM registry_schema_version") == 3
            assert [tuple(row) for row in await conn.fetch(
                "SELECT id, type FROM agent_runtimes WHERE type IN ('pi', 'dsh') ORDER BY id"
            )] == [('rt-dsh', 'dsh'), ('rt-pi', 'pi')]
            assert await conn.fetchval("SELECT count(*) FROM agent_profiles WHERE runtime_id IN ('rt-pi', 'rt-dsh')") == 0

    asyncio.run(asyncio.wait_for(run(), 45))


@pytest.mark.parametrize("finite_catalog", [False, True])
def test_migrated_user_model_on_builtin_account_survives_export_and_fresh_import(finite_catalog):
    from looper.agents import config_transfer

    async def run():
        async with _database() as old, _database() as fresh:
            await _legacy(old)
            if finite_catalog:
                await old.execute("UPDATE llm_providers SET models = $1 WHERE id = 'prov-claude-subscription'",
                                  json.dumps([{"name": "opus"}, {"name": "future-model"}]))
            await old.execute("INSERT INTO agent_profiles (id, name, runtime_id, provider_id, model) "
                              "VALUES ('agent-user-model', 'user-model-profile', 'rt-claude-code', "
                              "'prov-claude-subscription', 'future-model')")
            await connection.ensure_schema(old)
            saved = await old.fetchrow("SELECT m.id, m.builtin FROM llm_models m JOIN agent_profiles p "
                                      "ON p.model_id = m.id WHERE p.id = 'agent-user-model'")
            assert saved and saved['builtin'] is False
            assert await old.fetchval("SELECT builtin FROM llm_models WHERE provider_id = "
                                      "'prov-claude-subscription' AND name = 'opus'") is True
            payload = json.loads(''.join([part async for part in config_transfer.export_chunks(
                old, include_projects=False, include_secrets=False)]))
            assert any(model['name'] == 'future-model' for model in payload['models'])
            await connection.ensure_schema(fresh)
            async with fresh.transaction():
                result = await config_transfer.import_config(fresh, payload, apply=True)
            change = next(change for change in result.changes
                          if change.section == 'profiles' and change.key == 'user-model-profile')
            assert change.action is config_transfer.Action.ADDED
            imported = await fresh.fetchrow("SELECT p.name, m.name, m.provider_id, m.protocol FROM agent_profiles p "
                                           "JOIN llm_models m ON m.id = p.model_id WHERE p.name = 'user-model-profile'")
            assert tuple(imported) == ('user-model-profile', 'future-model', 'prov-claude-subscription',
                                       'anthropic-messages')

    asyncio.run(asyncio.wait_for(run(), 45))


@pytest.mark.parametrize("runtime,account_id,protocol", [
    ('claude-code', 'prov-claude-subscription', 'anthropic-messages'),
    ('codex', 'prov-codex-login', 'openai-responses'),
    ('grok', 'prov-grok-login', 'openai-responses'),
])
def test_apply_normalizes_legacy_oauth_protocols_without_changing_profile_or_session(runtime, account_id, protocol):
    async def run():
        async with _database() as conn:
            await _legacy(conn)
            await conn.execute("INSERT INTO agent_runtimes (id, name, type) VALUES ($1, $2, $2) "
                               "ON CONFLICT DO NOTHING", f'rt-{runtime}', runtime)
            await conn.execute("INSERT INTO llm_providers (id, name, auth, oauth_runtime_type, protocols, builtin) "
                               "VALUES ($1, $2, 'oauth', $3, ARRAY['anthropic-messages', 'openai-chat', "
                               "'openai-responses'], true) ON CONFLICT (id) DO UPDATE SET protocols = EXCLUDED.protocols",
                               account_id, account_id.removeprefix('prov-'), runtime)
            await conn.execute("INSERT INTO agent_profiles (id, name, runtime_id, provider_id, model) "
                               "VALUES ('agent-login', 'login-profile', $1, $2, 'future-model')",
                               f'rt-{runtime}', account_id)
            await conn.execute("INSERT INTO agent_sessions (agent_id, session_id, runtime_type, provider_id, profile_id) "
                               "VALUES ('login-agent', 'login-session', $1, $2, 'agent-login')", runtime, account_id)
            session = await conn.fetchrow("SELECT * FROM agent_sessions WHERE agent_id = 'login-agent'")
            expected = plan(await registry_migration._read_snapshot(conn))
            assert expected.applicable and expected.report['binding_mismatches'] == 0
            await connection.ensure_schema(conn)
            assert await conn.fetchrow("SELECT * FROM agent_sessions WHERE agent_id = 'login-agent'") == session
            saved = await conn.fetchrow("SELECT p.id, p.runtime_id, m.provider_id, m.name, m.protocol "
                                       "FROM agent_profiles p JOIN llm_models m ON m.id = p.model_id "
                                       "WHERE p.id = 'agent-login'")
            assert tuple(saved) == ('agent-login', f'rt-{runtime}', account_id, 'future-model', protocol)
            assert [tuple(row) for row in await conn.fetch(
                "SELECT protocol, base_url FROM llm_provider_endpoints WHERE provider_id = $1", account_id
            )] == [(protocol, '')]
            assert await conn.fetchval('SELECT version FROM registry_schema_version') == 3

    asyncio.run(asyncio.wait_for(run(), 45))


@pytest.mark.parametrize("runtime,protocol,model,effort,vendor,url", [
    ("pi", "openai-responses", "Exact.Model[1m]", "xhigh", "cpa", "https://cpa.example.invalid/v1/"),
    ("pi", "openai-chat", "Exact.Model[1m]", "low", "cpa", "https://cpa.example.invalid/v1/"),
    *[("dsh", "anthropic-messages", model, effort, "deepseek", "https://api.deepseek.com/anthropic")
      for model in ("deepseek-flash", "deepseek-flash[1m]", "deepseek-v4-pro", "deepseek-v4-pro[1m]")
      for effort in ("low", "high", "max")],
])
def test_apply_preserves_new_runtime_model_endpoint_effort_and_session(runtime, protocol, model, effort, vendor, url):
    async def run():
        async with _database() as conn:
            await _legacy(conn)
            await conn.execute("INSERT INTO agent_runtimes (id, name, type) VALUES ($1, $2, $2)",
                               f"rt-{runtime}", runtime)
            await conn.execute("UPDATE llm_providers SET protocols = $1, models = $2, vendor_type = $3, base_url = $4 "
                               "WHERE id IN ('prov-a', 'prov-b')",
                               [protocol], json.dumps([{"name": model, "context_window": 1_000_000,
                                                       "efforts": [effort], "input_usd_per_mtok": 0,
                                                       "output_usd_per_mtok": 2, "cache_read_usd_per_mtok": 0.1,
                                                       "cache_write_usd_per_mtok": 0.5}]), vendor, url)
            await conn.execute("UPDATE agent_profiles SET runtime_id = $1, model = $2, effort = $3 WHERE id = 'agent-test'",
                               f"rt-{runtime}", model, effort)
            await conn.execute("UPDATE agent_sessions SET runtime_type = $1", runtime)
            await conn.execute("UPDATE schedules SET planner_session_runtime = $1", runtime)
            expected = plan(await registry_migration._read_snapshot(conn))
            assert expected.applicable
            assert expected.report["binding_mismatches"] == expected.report["pricing_mismatches"] == 0
            records = await conn.fetch("SELECT * FROM llm_records")
            await connection.ensure_schema(conn)
            saved = await conn.fetchrow("SELECT p.id, p.runtime_id, p.effort, m.name, m.protocol, "
                                       "m.context_window, e.base_url, m.provider_id, p.agent_teams, p.custom_prompt, "
                                       "m.efforts, m.input_usd_per_mtok, m.output_usd_per_mtok, "
                                       "m.cache_read_usd_per_mtok, m.cache_write_usd_per_mtok FROM agent_profiles p "
                                       "JOIN llm_models m ON m.id = p.model_id "
                                       "JOIN llm_provider_endpoints e ON e.provider_id = m.provider_id "
                                       "AND e.protocol = m.protocol WHERE p.id = 'agent-test'")
            assert tuple(saved) == ('agent-test', f'rt-{runtime}', effort, model, protocol, 1_000_000,
                                    url, expected.conversion.provider_map['prov-b'], False, 'Keep this.',
                                    [effort], 0, 2, 0.1, 0.5)
            session = await conn.fetchrow("SELECT runtime_type, session_id, profile_id FROM agent_sessions")
            assert tuple(session) == (runtime, 'session-test', 'agent-test')
            assert await conn.fetch("SELECT * FROM llm_records") == records
            assert await conn.fetchval("SELECT version FROM registry_schema_version") == 3
            before = await state(conn)
            await connection.ensure_schema(conn)
            assert await state(conn) == before

    asyncio.run(asyncio.wait_for(run(), 45))
