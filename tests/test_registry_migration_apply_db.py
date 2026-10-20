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
