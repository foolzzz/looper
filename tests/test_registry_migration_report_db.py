"""Count-only static preflight against a disposable PostgreSQL legacy registry."""

import asyncio
import json
import os
import uuid

import asyncpg
import pytest

from looper.agents.registry_migration import plan
from looper.db.registry_migration import dry_run, read_snapshot
from tests.test_registry_migration import REPORT_FIELDS


DSN = os.environ.get("LOOPER_TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")

LEGACY = """
CREATE TABLE agent_runtimes (id text, name text, type text, executable text, capabilities text[]);
CREATE TABLE llm_providers (id text, name text, auth text, protocols text[], base_url text,
    models jsonb, env jsonb, vendor_type text, created_at timestamptz);
CREATE TABLE llm_provider_secrets (provider_id text, api_key text);
CREATE TABLE agent_profiles (id text, name text, runtime_id text, provider_id text, model text, effort text);
CREATE TABLE agent_sessions (provider_id text, session_id text);
CREATE TABLE schedules (planner_session_provider text, planner_session_id text);
INSERT INTO agent_runtimes VALUES ('rt-omp', 'private-runtime', 'omp', '', '{}');
INSERT INTO llm_providers VALUES
    ('prov-a', 'private-account-a', 'api_key', '{openai-responses}', 'https://private.invalid',
     '[]', '[{"key":"PRIVATE_SETTING","value":"private-env-value"}]', 'deepseek', '2026-01-01'),
    ('prov-b', 'private-account-b', 'api_key', '{openai-responses}', 'https://private.invalid',
     '[]', '[{"key":"PRIVATE_SETTING","value":"private-env-value"}]', 'deepseek', '2026-01-02');
INSERT INTO llm_provider_secrets VALUES ('prov-a', 'private-test-key'), ('prov-b', 'private-test-key');
INSERT INTO agent_profiles VALUES ('agent-private', 'private-profile', 'rt-omp', 'prov-b', 'private-model', 'high');
INSERT INTO agent_sessions VALUES ('prov-b', 'private-session');
INSERT INTO schedules VALUES ('prov-b', 'private-schedule-session');
"""


async def state(conn):
    columns = await conn.fetch("SELECT table_name, column_name, data_type, column_default, is_nullable "
                               "FROM information_schema.columns WHERE table_schema = current_schema() "
                               "ORDER BY table_name, ordinal_position")
    tables = await conn.fetch("SELECT tablename FROM pg_tables WHERE schemaname = current_schema() ORDER BY tablename")
    rows = {r["tablename"]: await conn.fetch(f'SELECT to_jsonb(t) FROM "{r["tablename"]}" t ORDER BY to_jsonb(t)::text')
            for r in tables}
    return columns, rows


@pytest.mark.parametrize("bad_profile", [False, True])
def test_readonly_preflight_preserves_schema_rows_and_hides_all_values(bad_profile):
    async def run():
        conn = await asyncpg.connect(DSN, command_timeout=10)
        schema = f"looper_test_{uuid.uuid4().hex}"
        try:
            await conn.execute(f'CREATE SCHEMA "{schema}"')
            await conn.execute(f'SET search_path TO "{schema}"')
            await conn.execute(LEGACY)
            if bad_profile:
                await conn.execute("UPDATE agent_profiles SET runtime_id = 'missing'")
            before = await state(conn)
            # The convenience entry opens its own consistent read-only transaction.
            standalone = await dry_run(conn)
            async with conn.transaction():
                with pytest.raises(ValueError, match="read-only"):
                    await dry_run(conn)
            async with conn.transaction(readonly=True):
                with pytest.raises(ValueError, match="repeatable-read"):
                    await dry_run(conn)
            async with conn.transaction(isolation="repeatable_read", readonly=True):
                old = await read_snapshot(conn)
                assert old.providers and old.profiles
                result = plan(old)
                report = await dry_run(conn)
                assert report == standalone
                assert report == result.report
                assert report.keys() == REPORT_FIELDS
                assert result.applicable is not bad_profile
                assert report["profiles_before"] == 1
                assert report["unresolved_profiles"] == int(bad_profile)
                if not bad_profile:
                    assert report["profiles_mapped"] == 1
                    assert report["providers_merged"] == 1
                    assert report["session_references_remapped"] == 2
                encoded = json.dumps(report)
                for value in ("private", "prov-a", "prov-b", "rt-omp", "https://", "PRIVATE_SETTING"):
                    assert value not in encoded
                for key, value in report.items():
                    if key == "providers_kept_separate_by_reason":
                        assert all(type(n) is int and n >= 0 for n in value.values())
                    else:
                        assert type(value) is int and value >= 0
            assert await state(conn) == before
        finally:
            await conn.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
            await conn.close()

    asyncio.run(asyncio.wait_for(run(), timeout=30))
