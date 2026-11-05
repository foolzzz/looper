"""Fresh and released registry baselines admit only Cursor's OAuth pairing."""

import asyncio
import uuid

import asyncpg
import pytest

from looper.agents.runtimes import cursor_login
from looper.agents import self_check
from looper.db import connection
from tests.bridge_schema_support import structure
from tests.db_support import DSN, fresh_connection
from tests.db_support import previous_login_schema as previous_login_schema

pytestmark = pytest.mark.skipif(not DSN, reason="requires disposable LOOPER_TEST_DATABASE_URL")


async def _assert_cursor_provider(conn):
    row = await conn.fetchrow("SELECT vendor_type, auth, oauth_runtime_type, builtin FROM llm_providers "
                              "WHERE id='prov-cursor-login'")
    assert tuple(row) == ("cursor-login", "oauth", "cursor-agent", True)
    assert await conn.fetchval("SELECT base_url FROM llm_provider_endpoints "
                               "WHERE provider_id='prov-cursor-login' AND protocol='openai-responses'") == ""
    models = await conn.fetch("SELECT id, name, protocol, builtin, input_usd_per_mtok, output_usd_per_mtok "
                              "FROM llm_models WHERE provider_id='prov-cursor-login' ORDER BY catalog_order")
    assert [row["name"] for row in models] == list(cursor_login.SUGGESTED_MODELS)
    assert all(uuid.UUID(row["id"].removeprefix("model-")).version == 5
               and row["protocol"] == "openai-responses" and row["builtin"]
               and row["input_usd_per_mtok"] is None and row["output_usd_per_mtok"] is None for row in models)
    for auth, runtime in (("api_key", None), ("oauth", "grok"), ("oauth", None)):
        with pytest.raises(asyncpg.CheckViolationError):
            await conn.execute("UPDATE llm_providers SET auth=$1, oauth_runtime_type=$2 WHERE id='prov-cursor-login'",
                               auth, runtime)


def test_fresh_registry_seeds_cursor_login(monkeypatch):
    async def test():
        async with fresh_connection() as conn:
            await connection.ensure_schema(conn)
            await _assert_cursor_provider(conn)
            assert not await connection.schema_change_pending(conn)
            await conn.execute("INSERT INTO agent_runtimes (id, name, type) VALUES ('cursor', 'cursor', 'cursor-agent')")
            await conn.execute("INSERT INTO agent_profiles (id, name, runtime_id, model_id) "
                               "SELECT 'cursor', 'cursor', 'cursor', id FROM llm_models "
                               "WHERE provider_id='prov-cursor-login' AND name='auto'")
            schema = await conn.fetchval("SELECT current_schema()")
            connect = asyncpg.connect

            async def isolated_connect(dsn, **kwargs):
                return await connect(dsn, **kwargs, server_settings={"search_path": schema})

            monkeypatch.setattr(asyncpg, "connect", isolated_connect)
            facts = await self_check._database_facts(str(DSN))
            assert facts.cursor_login_profiles == 1
    asyncio.run(asyncio.wait_for(test(), 30))


def test_released_registry_upgrades_cursor_checks_once(previous_login_schema):
    async def test():
        async with fresh_connection() as old, fresh_connection() as fresh:
            await old.execute(previous_login_schema)
            await old.execute("INSERT INTO schema_migrations (name) VALUES ('post_bridge_baseline')")
            await old.execute("INSERT INTO registry_schema_version (id, version) VALUES (TRUE, 4)")
            # Positive control: the old checks accept Grok, but reject Cursor.
            await old.execute("INSERT INTO llm_providers (id, name, vendor_type, auth, oauth_runtime_type) "
                              "VALUES ('saved', 'Saved Grok', 'grok-login', 'oauth', 'grok')")
            before = dict(await old.fetchrow("SELECT * FROM llm_providers WHERE id='saved'"))
            with pytest.raises(asyncpg.CheckViolationError):
                await old.execute("INSERT INTO llm_providers (id, name, vendor_type, auth, oauth_runtime_type) "
                                  "VALUES ('cursor-old', 'Cursor', 'cursor-login', 'oauth', 'cursor-agent')")
            assert await connection.schema_change_pending(old)
            await connection.ensure_schema(old)
            await _assert_cursor_provider(old)
            assert dict(await old.fetchrow("SELECT * FROM llm_providers WHERE id='saved'")) == {**before, "enabled": True}
            await connection.ensure_schema(fresh)
            assert await structure(old) == await structure(fresh)
            marker = await old.fetchrow("SELECT * FROM schema_migrations WHERE name='cursor_login'")
            assert marker is not None and not await connection.schema_change_pending(old)
            await old.execute("UPDATE llm_models SET context_window=12345 WHERE provider_id='prov-cursor-login'")
            await connection.ensure_schema(old)
            assert await old.fetchrow("SELECT * FROM schema_migrations WHERE name='cursor_login'") == marker
            assert await old.fetchval("SELECT min(context_window) FROM llm_models WHERE provider_id='prov-cursor-login'") == 12345
    asyncio.run(asyncio.wait_for(test(), 30))


@pytest.mark.parametrize("occupied_names", [
    ("cursor-login",),
    ("cursor-login", "cursor-login (1)", "cursor-login (2)"),
])
def test_released_registry_preserves_custom_cursor_login_name(previous_login_schema, occupied_names):
    async def test():
        async with fresh_connection() as conn:
            await conn.execute(previous_login_schema)
            await conn.execute("INSERT INTO schema_migrations (name) VALUES ('post_bridge_baseline')")
            await conn.execute("INSERT INTO registry_schema_version (id, version) VALUES (TRUE, 4)")
            await conn.executemany(
                "INSERT INTO llm_providers (id, name, vendor_type, auth, max_concurrency) "
                "VALUES ($1, $2, 'custom', 'api_key', 2)",
                [(f"saved-{i}", name) for i, name in enumerate(occupied_names)])
            await conn.execute("""
                INSERT INTO llm_provider_secrets (provider_id, api_key) VALUES ('saved-0', 'synthetic-key');
                INSERT INTO llm_provider_endpoints (provider_id, protocol, base_url)
                    VALUES ('saved-0', 'anthropic-messages', 'https://api.example.invalid/v1');
                INSERT INTO llm_models (id, provider_id, name, protocol, context_window)
                    VALUES ('model-saved', 'saved-0', 'auto', 'anthropic-messages', 12345);
                INSERT INTO agent_runtimes (id, name, type) VALUES ('saved-runtime', 'saved-runtime', 'claude-code');
                INSERT INTO agent_profiles (id, name, runtime_id, model_id)
                    VALUES ('saved-profile', 'saved-profile', 'saved-runtime', 'model-saved');
            """)
            queries = ["SELECT * FROM llm_providers WHERE id LIKE 'saved-%' ORDER BY id",
                       "SELECT * FROM llm_provider_secrets WHERE provider_id='saved-0'",
                       "SELECT * FROM llm_provider_endpoints WHERE provider_id='saved-0'",
                       "SELECT * FROM llm_models WHERE id='model-saved'",
                       "SELECT * FROM agent_profiles WHERE id='saved-profile'"]
            before = [[dict(row) for row in await conn.fetch(query)] for query in queries]
            # The availability migration enables existing accounts without changing saved fields.
            expected = [[{**row, "enabled": True} for row in before[0]], *before[1:]]
            for _ in range(2):  # Upgrade, then restart through the same startup path.
                await connection.ensure_schema(conn)
                await _assert_cursor_provider(conn)
                assert [[dict(row) for row in await conn.fetch(query)] for query in queries] == expected
                assert await conn.fetchval("SELECT name FROM llm_providers WHERE id='prov-cursor-login'") == (
                    f"cursor-login ({len(occupied_names)})")
                assert not await connection.schema_change_pending(conn)
                snapshot = await conn.fetch("SELECT * FROM llm_providers WHERE id='prov-cursor-login'")
                if _ == 0:
                    seeded = snapshot
                else:
                    assert snapshot == seeded
    asyncio.run(asyncio.wait_for(test(), 30))
