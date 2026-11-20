"""Runtime capability refresh survives fresh starts and existing-schema upgrades."""

import asyncio

import asyncpg
import pytest

from looper.agents import profile_registry, runtimes
from looper.db import connection
from tests.db_support import DSN, fresh_connection

pytestmark = pytest.mark.skipif(not DSN, reason="requires disposable LOOPER_TEST_DATABASE_URL")


@pytest.mark.parametrize("upgraded", [False, True], ids=["fresh", "upgraded"])
def test_startup_refresh_admits_headless_compaction_and_continues_registration(monkeypatch, upgraded):
    async def detected(adapter, launcher):
        if adapter.type == "codex" and launcher.value == "native":
            return {"name": "Codex", "type": "codex", "launcher": "native", "executable": "",
                    "version": "test", "capabilities": ["run"], "probe_error": None}
        return None

    async def test():
        async with fresh_connection() as conn:
            await connection.ensure_schema(conn)
            monkeypatch.setattr(connection, "_pool", conn)
            monkeypatch.setattr(profile_registry, "_detected_runtime", detected)
            if upgraded:
                await conn.execute("UPDATE agent_runtimes SET capabilities=array_remove(capabilities, "
                                   "'headless_compaction')")
                constraint = await conn.fetchval("SELECT pg_get_constraintdef(oid) FROM pg_constraint "
                    "WHERE conrelid='agent_runtimes'::regclass AND conname='agent_runtimes_capabilities_check'")
                await conn.execute("ALTER TABLE agent_runtimes DROP CONSTRAINT agent_runtimes_capabilities_check, "
                    "ADD CONSTRAINT agent_runtimes_capabilities_check "
                    + constraint.replace(", 'headless_compaction'::text", ""))
                assert await connection.schema_change_pending(conn)
                async with conn.transaction():
                    await connection.ensure_schema(conn)
                    assert not await conn.fetchval("SELECT convalidated FROM pg_constraint "
                        "WHERE conrelid='agent_runtimes'::regclass AND conname='agent_runtimes_capabilities_check'")
                await connection.ensure_schema(conn)
            assert "Codex" in await profile_registry.register_found_runtimes()
            caps = await conn.fetchval("SELECT capabilities FROM agent_runtimes WHERE id='rt-claude-code'")
            assert caps == sorted(c.value for c in runtimes.CLAUDE_CODE.capabilities)
            assert "headless_compaction" in caps
            with pytest.raises(asyncpg.CheckViolationError):
                await conn.execute("UPDATE agent_runtimes SET capabilities=ARRAY['unknown'] WHERE id='rt-codex'")
            assert await conn.fetchval("SELECT convalidated FROM pg_constraint "
                "WHERE conrelid='agent_runtimes'::regclass AND conname='agent_runtimes_capabilities_check'")
            assert not await connection.schema_change_pending(conn)
            before = await conn.fetch("SELECT name FROM schema_migrations ORDER BY name")
            await connection.ensure_schema(conn)
            assert await conn.fetch("SELECT name FROM schema_migrations ORDER BY name") == before
    asyncio.run(asyncio.wait_for(test(), 30))
