"""Runtime launchers survive registry reads and additive schema upgrades."""

import asyncio

import asyncpg
import pytest

from looper.db import config_transfer, connection, profile_registry
from tests.bridge_schema_support import structure
from tests.db_support import DSN, fresh_connection, run_db

pytestmark = pytest.mark.skipif(not DSN, reason="requires disposable LOOPER_TEST_DATABASE_URL")


def test_existing_runtime_rows_upgrade_once_and_keep_launcher_choices():
    async def test():
        async with fresh_connection() as old, fresh_connection() as fresh:
            column = "    launcher      TEXT NOT NULL DEFAULT 'native' CHECK (launcher IN ('native', 'ttadk')),\n"
            sql = connection.SCHEMA_PATH.read_text()
            assert sql.count(column) == 1
            await old.execute(sql.replace(column, ""))
            await old.execute("INSERT INTO schema_migrations (name) VALUES ('post_bridge_baseline')")
            await old.execute("INSERT INTO registry_schema_version (id, version) VALUES (TRUE, 4)")
            await old.execute(connection.SEEDS_PATH.read_text())
            before = dict(await old.fetchrow("SELECT * FROM agent_runtimes WHERE id='rt-claude-code'"))
            assert await connection.schema_change_pending(old)
            assert dict(await old.fetchrow("SELECT * FROM agent_runtimes WHERE id='rt-claude-code'")) == before
            await connection.ensure_schema(old)
            assert dict(await old.fetchrow("SELECT * FROM agent_runtimes WHERE id='rt-claude-code'")) == {
                **before, "launcher": "native"}
            await connection.ensure_schema(fresh)
            assert await structure(old) == await structure(fresh)
            marker = await old.fetchrow("SELECT * FROM schema_migrations WHERE name='runtime_launcher'")
            assert marker is not None
            await old.execute("UPDATE agent_runtimes SET launcher='ttadk'")
            assert not await connection.schema_change_pending(old)
            await connection.ensure_schema(old)
            assert await old.fetchval("SELECT launcher FROM agent_runtimes WHERE id='rt-claude-code'") == "ttadk"
            assert await old.fetchrow("SELECT * FROM schema_migrations WHERE name='runtime_launcher'") == marker
            for value in (None, "other"):
                with pytest.raises((asyncpg.NotNullViolationError, asyncpg.CheckViolationError)):
                    await old.execute("UPDATE agent_runtimes SET launcher=$1", value)

    asyncio.run(asyncio.wait_for(test(), 30))


def test_launcher_registry_reads_count_only_profiles_using_that_launcher(monkeypatch):
    async def test(pool):
        assert await profile_registry.launcher_profile_count("ttadk") == 0
        fields = {"name": "ttadk-codex", "type": "codex", "launcher": "ttadk", "executable": "",
                  "version": "ttadk test / codex test", "capabilities": ["run"], "probe_error": None}
        assert await profile_registry.insert_builtin_runtime("rt-ttadk-codex", fields)
        await pool.execute("INSERT INTO agent_profiles (id,name,runtime_id,model_id) "
                           "SELECT 'agent-ttadk','TTADK','rt-ttadk-codex',id FROM llm_models "
                           "WHERE provider_id='prov-codex-login' AND name='gpt-6.1-sol'")
        assert await profile_registry.launcher_profile_count("ttadk") == 1
        assert await profile_registry.launcher_profile_count("native") == 1
        profile = await profile_registry.get_profile("agent-ttadk")
        assert (profile["runtime_launcher"], profile["runtime_version"]) == ("ttadk", fields["version"])
        assert profile == next(row for row in await profile_registry.list_profiles() if row["id"] == "agent-ttadk")
        async with pool.acquire() as conn:
            rows = await config_transfer.read_runtimes(conn)
        assert next(row for row in rows if row["id"] == "rt-ttadk-codex")["launcher"] == "ttadk"

    run_db(test, monkeypatch, timeout=30)
