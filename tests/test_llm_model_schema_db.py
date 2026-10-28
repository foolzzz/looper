"""Target registry constraints and bridge-only version guards on disposable PostgreSQL."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from pathlib import Path

import asyncpg
import pytest

from looper.db import connection
from tests.db_support import DSN, fresh_connection as _database

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")



def _run(test: Callable[[asyncpg.Connection], Awaitable[None]], *, initialize: bool = True) -> None:
    async def run() -> None:
        async with _database() as conn:
            if initialize:
                await connection.ensure_schema(conn)
            await test(conn)

    asyncio.run(asyncio.wait_for(run(), timeout=45))


async def _account(conn: asyncpg.Connection) -> None:
    await conn.execute("INSERT INTO llm_providers (id, name, vendor_type, auth) "
                       "VALUES ('prov-test', 'test', 'custom', 'api_key')")
    await conn.execute("INSERT INTO llm_provider_endpoints (provider_id, protocol, base_url) "
                       "VALUES ('prov-test', 'openai-chat', 'https://example.invalid/v1')")


async def _model(conn: asyncpg.Connection) -> None:
    await _account(conn)
    await conn.execute("INSERT INTO llm_models (id, provider_id, name, protocol) "
                       "VALUES ('model-test', 'prov-test', 'Exact.Model[1m]', 'openai-chat')")


def test_fresh_target_schema_and_second_initialization() -> None:
    async def test(conn: asyncpg.Connection) -> None:
        assert await connection.schema_change_pending(conn) is False
        await connection.ensure_schema(conn)
        assert await conn.fetchval("SELECT version FROM registry_schema_version") == 4
        columns = await conn.fetch("SELECT table_name, column_name FROM information_schema.columns "
                                   "WHERE table_schema = current_schema()")
        profile = {r['column_name'] for r in columns if r['table_name'] == 'agent_profiles'}
        assert profile == {'id', 'name', 'runtime_id', 'model_id', 'effort', 'agent_teams', 'custom_prompt',
                           'builtin', 'created_at', 'updated_at'}
        provider = {r['column_name'] for r in columns if r['table_name'] == 'llm_providers'}
        assert 'vendor_type' in provider and not provider & {'protocols', 'base_url', 'models'}
        assert await conn.fetchval("SELECT count(*) FROM llm_model_migration_map") == 0
        assert await conn.fetchval("SELECT model_id FROM agent_profiles "
                                   "WHERE id = 'agent-claude-code-opus'") in {
            r['id'] for r in await conn.fetch("SELECT id FROM llm_models WHERE provider_id = "
                                             "'prov-claude-subscription' AND name = 'opus' "
                                             "AND protocol = 'anthropic-messages'")}
        for table in ('agent_setting_layers', 'task_system_prompt_modes'):
            assert any(r['table_name'] == table and r['column_name'] == 'system_prompt_mode' for r in columns)
        tables = ('llm_providers', 'llm_provider_endpoints', 'llm_models', 'agent_profiles',
                  'registry_schema_version', 'schema_fingerprint')
        before = {table: await conn.fetch(f'SELECT * FROM {table} ORDER BY 1, 2') for table in tables}
        await connection.ensure_schema(conn)
        assert {table: await conn.fetch(f'SELECT * FROM {table} ORDER BY 1, 2') for table in tables} == before
        assert await connection.schema_change_pending(conn) is False

    _run(test, initialize=False)


@pytest.mark.parametrize('provider,protocol', [
    ('prov-claude-subscription', 'anthropic-messages'),
    ('prov-codex-login', 'openai-responses'), ('prov-grok-login', 'openai-responses'),
])
def test_login_endpoints_have_fixed_natural_keys(provider: str, protocol: str) -> None:
    async def test(conn: asyncpg.Connection) -> None:
        row = await conn.fetchrow("SELECT base_url, use_runtime_default FROM llm_provider_endpoints "
                                  "WHERE provider_id = $1 AND protocol = $2", provider, protocol)
        assert dict(row) == {'base_url': '', 'use_runtime_default': False}
        assert await conn.fetchval("SELECT count(*) FROM llm_provider_endpoints WHERE provider_id = $1", provider) == 1
        models = await conn.fetch("SELECT id, name, builtin FROM llm_models WHERE provider_id = $1", provider)
        assert models and all(row['id'].startswith('model-') and row['builtin'] for row in models)

    _run(test)


def test_builtin_model_natural_key_preserves_existing_id_and_metadata() -> None:
    async def test(conn: asyncpg.Connection) -> None:
        await conn.execute("UPDATE llm_models SET id = 'model-existing', context_window = 123, efforts = '{}' "
                           "WHERE provider_id = 'prov-codex-login' AND name = 'gpt-6.1-sol'")
        await connection.ensure_schema(conn)
        row = await conn.fetchrow("SELECT id, context_window, efforts FROM llm_models "
                                  "WHERE provider_id = 'prov-codex-login' AND name = 'gpt-6.1-sol' "
                                  "AND protocol = 'openai-responses'")
        assert dict(row) == {'id': 'model-existing', 'context_window': 123, 'efforts': []}

    _run(test)


def test_registry_checks_reject_invalid_rows_on_one_migrated_schema() -> None:
    async def test(conn: asyncpg.Connection) -> None:
        await _model(conn)
        # A failed statement rolls back by itself; every check starts from the same valid rows.
        for assignment in ("vendor_type = 'unknown'", "vendor_type = 'codex-login'",
                           "auth = 'oauth', oauth_runtime_type = 'codex'", 'vendor_type = NULL'):
            with pytest.raises((asyncpg.CheckViolationError, asyncpg.NotNullViolationError)):
                await conn.execute(f"UPDATE llm_providers SET {assignment} WHERE id = 'prov-test'")
        for assignment in ("protocol = 'unknown'", "base_url = 'ftp://example.invalid'",
                           "base_url = 'https://example.invalid/has space'",
                           "base_url = 'https://user:secret@example.invalid'", 'use_runtime_default = TRUE'):
            with pytest.raises(asyncpg.CheckViolationError):
                await conn.execute(f"UPDATE llm_provider_endpoints SET {assignment} WHERE provider_id = 'prov-test'")
        invalid_models = [
            ('id', 'bad-id'), ('name', ''), ('name', '-flag'), ('name', 'has space'),
            ('name', 'a' * 201), ('protocol', 'unknown'), ('context_window', 0),
            ('context_window', 100_000_001), ('catalog_order', -1), ('background_model', 'bad name'),
            ('efforts', ['high', 'high']), ('efforts', ['High']), ('efforts', [None]),
            ('efforts', ['e' * 33]), ('efforts', [f'level{i}' for i in range(11)]),
        ] + [(field, value) for field in (
            'input_usd_per_mtok', 'output_usd_per_mtok', 'cache_read_usd_per_mtok', 'cache_write_usd_per_mtok'
        ) for value in (-1, 10_000.01, float('nan'), float('inf'), -float('inf'))]
        for field, value in invalid_models:
            with pytest.raises(asyncpg.CheckViolationError):
                await conn.execute(f'UPDATE llm_models SET {field} = $1 WHERE id = $2', value, 'model-test')

    _run(test)


def test_model_identity_endpoint_fk_and_restricted_deletes() -> None:
    async def test(conn: asyncpg.Connection) -> None:
        await _model(conn)
        with pytest.raises(asyncpg.UniqueViolationError):
            await conn.execute("INSERT INTO llm_models (id, provider_id, name, protocol) "
                               "VALUES ('model-duplicate', 'prov-test', 'Exact.Model[1m]', 'openai-chat')")
        with pytest.raises(asyncpg.UniqueViolationError):
            await conn.execute("INSERT INTO llm_provider_endpoints (provider_id, protocol, base_url) "
                               "VALUES ('prov-test', 'openai-chat', 'https://other.invalid')")
        with pytest.raises(asyncpg.ForeignKeyViolationError):
            await conn.execute("INSERT INTO llm_models (id, provider_id, name, protocol) "
                               "VALUES ('model-no-endpoint', 'prov-test', 'm', 'openai-responses')")
        await conn.execute("INSERT INTO llm_provider_endpoints (provider_id, protocol, base_url) "
                           "VALUES ('prov-test', 'openai-responses', 'https://other.invalid')")
        await conn.execute("INSERT INTO llm_models (id, provider_id, name, protocol) "
                           "VALUES ('model-other-protocol', 'prov-test', 'Exact.Model[1m]', 'openai-responses')")
        await conn.execute("INSERT INTO llm_models (id, provider_id, name, protocol) "
                           "VALUES ('model-case', 'prov-test', 'exact.Model[1m]', 'openai-chat')")
        await conn.execute("INSERT INTO llm_providers (id, name, vendor_type, auth) "
                           "VALUES ('prov-other', 'other', 'custom', 'api_key')")
        await conn.execute("INSERT INTO llm_provider_endpoints (provider_id, protocol, base_url) "
                           "VALUES ('prov-other', 'openai-chat', 'https://example.invalid')")
        await conn.execute("INSERT INTO llm_models (id, provider_id, name, protocol) "
                           "VALUES ('model-other-provider', 'prov-other', 'Exact.Model[1m]', 'openai-chat')")
        with pytest.raises(asyncpg.ForeignKeyViolationError):
            await conn.execute("INSERT INTO agent_profiles (id, name, runtime_id, model_id) "
                               "VALUES ('agent-test', 'test', 'rt-claude-code', 'model-missing')")
        await conn.execute("INSERT INTO agent_profiles (id, name, runtime_id, model_id) "
                           "VALUES ('agent-test', 'test', 'rt-claude-code', 'model-test')")
        with pytest.raises(asyncpg.NotNullViolationError):
            await conn.execute("UPDATE agent_profiles SET model_id = NULL WHERE id = 'agent-test'")
        for sql in ("DELETE FROM llm_models WHERE id = 'model-test'",
                    "DELETE FROM llm_provider_endpoints WHERE provider_id = 'prov-test'",
                    "DELETE FROM llm_providers WHERE id = 'prov-test'"):
            with pytest.raises(asyncpg.ForeignKeyViolationError):
                await conn.execute(sql)
        await conn.execute("UPDATE llm_models SET efforts = '{}', context_window = 100000000, "
                           "input_usd_per_mtok = 0, output_usd_per_mtok = 10000, "
                           "background_model = 'outside-catalog' WHERE id = 'model-test'")
        assert await conn.fetchval("SELECT efforts FROM llm_models WHERE id = 'model-test'") == []

    _run(test)


def test_migration_map_is_a_snapshot_without_live_foreign_keys() -> None:
    async def test(conn: asyncpg.Connection) -> None:
        await _model(conn)
        sql = ("INSERT INTO llm_model_migration_map "
               "(legacy_provider_id, legacy_model, protocol, provider_id, model_id) "
               "VALUES ('old-provider', 'Exact.Model[1m]', 'openai-chat', 'prov-test', 'model-test')")
        await conn.execute(sql)
        with pytest.raises(asyncpg.UniqueViolationError):
            await conn.execute(sql)
        await conn.execute("DELETE FROM llm_models WHERE id = 'model-test'")
        await conn.execute("DELETE FROM llm_providers WHERE id = 'prov-test'")
        assert await conn.fetchval("SELECT model_id FROM llm_model_migration_map") == 'model-test'
        assert await conn.fetchval("SELECT count(*) FROM llm_provider_endpoints WHERE provider_id = 'prov-test'") == 0

    _run(test)

def test_v2_forward_guard_accepts_its_own_version_and_empty_marker_is_refused() -> None:
    async def test(conn: asyncpg.Connection) -> None:
        await conn.execute("CREATE TABLE registry_schema_version (id BOOLEAN PRIMARY KEY CHECK(id), "
                           "version INTEGER NOT NULL CHECK(version > 0)); "
                           "INSERT INTO registry_schema_version VALUES (TRUE, 2)")
        assert await connection.check_registry_schema_version(conn, supported_version=2) == 2
        await conn.execute("DELETE FROM registry_schema_version")
        with pytest.raises(connection.RegistrySchemaVersionError, match='missing'):
            await connection.ensure_schema(conn)
        assert await conn.fetchval("SELECT to_regclass('schema_migrations')") is None

    _run(test, initialize=False)


@pytest.mark.parametrize('entry', ['ensure_schema', 'schema_change_pending'])
def test_forward_guard_rejects_v3_before_ddl_with_v2_support(entry: str, monkeypatch: pytest.MonkeyPatch,
                                                           tmp_path) -> None:
    async def test(conn: asyncpg.Connection) -> None:
        await conn.execute("CREATE TABLE registry_schema_version (id BOOLEAN PRIMARY KEY CHECK(id), "
                           "version INTEGER NOT NULL CHECK(version > 0)); "
                           "INSERT INTO registry_schema_version VALUES (TRUE, 3)")
        # Broken SQL would fail first if either public entry point ran DDL before its gate.
        sql = tmp_path / 'schema.sql'
        sql.write_text('CREATE TABLE ddl_must_not_run (id integer); invalid SQL;', encoding='utf-8')
        monkeypatch.setattr(connection, 'SCHEMA_PATH', sql)
        monkeypatch.setattr(connection, 'REGISTRY_SCHEMA_VERSION', 2)
        with pytest.raises(connection.RegistrySchemaVersionError, match='3.*2'):
            await getattr(connection, entry)(conn)
        assert await conn.fetchval("SELECT to_regclass('ddl_must_not_run')") is None
        assert await conn.fetchval("SELECT to_regclass('schema_migrations')") is None
        assert await conn.fetchval("SELECT version FROM registry_schema_version") == 3

    _run(test, initialize=False)


@pytest.mark.parametrize('version', [None, 2])
@pytest.mark.parametrize('entry', ['ensure_schema', 'schema_change_pending'])
def test_pre_bridge_database_is_refused_without_mutation(version: int | None, entry: str) -> None:
    async def test(conn: asyncpg.Connection) -> None:
        await conn.execute((Path(__file__).parent / 'fixtures' / 'schema_v2_6_0.sql').read_text())
        if version is not None:
            await conn.execute("CREATE TABLE registry_schema_version (id BOOLEAN PRIMARY KEY CHECK(id), "
                               "version INTEGER NOT NULL CHECK(version > 0)); "
                               "INSERT INTO registry_schema_version VALUES (TRUE, 2)")
        before = await conn.fetch("SELECT * FROM llm_providers ORDER BY id")
        with pytest.raises(connection.RegistrySchemaVersionError, match=r'v2\.13\.165'):
            await getattr(connection, entry)(conn)
        assert await conn.fetch("SELECT * FROM llm_providers ORDER BY id") == before
        assert await conn.fetchval("SELECT to_regclass('agents') IS NOT NULL")
        for table in ('llm_models', 'llm_provider_endpoints', 'llm_model_migration_map', 'schema_fingerprint'):
            assert await conn.fetchval('SELECT to_regclass($1)', table) is None
        assert await conn.fetchval("SELECT to_regclass('registry_schema_version') IS NOT NULL") == (version is not None)

    _run(test, initialize=False)
