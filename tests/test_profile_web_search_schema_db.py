"""Existing Agent Profiles keep web search enabled through the additive upgrade."""

import asyncio

import pytest

from looper.db import connection
from tests.bridge_schema_support import structure
from tests.db_support import DSN, fresh_connection

pytestmark = pytest.mark.skipif(not DSN, reason="requires disposable LOOPER_TEST_DATABASE_URL")


def test_existing_profiles_upgrade_with_web_search_enabled_and_preserve_saved_value():
    async def test():
        async with fresh_connection() as old, fresh_connection() as fresh:
            column = "    web_search BOOLEAN NOT NULL DEFAULT true,\n"
            sql = connection.SCHEMA_PATH.read_text()
            assert sql.count(column) == 1
            await old.execute(sql.replace(column, ""))
            await old.execute("INSERT INTO schema_migrations (name) VALUES ('post_bridge_baseline')")
            await old.execute("INSERT INTO registry_schema_version (id, version) VALUES (TRUE, 4)")
            await old.execute(connection.SEEDS_PATH.read_text())
            before = [dict(row) for row in await old.fetch("SELECT * FROM agent_profiles ORDER BY id")]
            assert before
            assert await connection.schema_change_pending(old)
            await connection.ensure_schema(old)
            assert [dict(row) for row in await old.fetch("SELECT * FROM agent_profiles ORDER BY id")] == [
                {**row, "web_search": True} for row in before]
            await connection.ensure_schema(fresh)
            assert await structure(old) == await structure(fresh)
            await old.execute("UPDATE agent_profiles SET web_search = false")
            assert not await connection.schema_change_pending(old)
            await connection.ensure_schema(old)
            assert await old.fetchval("SELECT bool_or(web_search) FROM agent_profiles") is False
    asyncio.run(asyncio.wait_for(test(), 30))
