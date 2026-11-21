"""Only the verified runtime admits input during a resident turn."""

import asyncio

import asyncpg
import pytest

from looper.agents.runtimes import ADAPTERS, Capability
from looper.db import connection
from tests.db_support import DSN, fresh_connection


def test_only_claude_code_declares_mid_turn_input():
    assert [name for name, adapter in ADAPTERS.items()
            if Capability.MID_TURN_INPUT in adapter.capabilities] == ["claude-code"]


@pytest.mark.skipif(not DSN, reason="requires disposable LOOPER_TEST_DATABASE_URL")
@pytest.mark.parametrize("upgraded", [False, True], ids=["fresh", "upgrade"])
def test_mid_turn_input_registry_constraint(upgraded):
    async def test():
        async with fresh_connection() as conn:
            await connection.ensure_schema(conn)
            if upgraded:
                await conn.execute("UPDATE agent_runtimes SET capabilities=array_remove(capabilities, 'mid_turn_input')")
                constraint = await conn.fetchval("SELECT pg_get_constraintdef(oid) FROM pg_constraint "
                    "WHERE conrelid='agent_runtimes'::regclass AND conname='agent_runtimes_capabilities_check'")
                await conn.execute("ALTER TABLE agent_runtimes DROP CONSTRAINT agent_runtimes_capabilities_check, "
                    "ADD CONSTRAINT agent_runtimes_capabilities_check "
                    + constraint.replace(", 'mid_turn_input'::text", ""))
                assert await connection.schema_change_pending(conn)
                async with conn.transaction():
                    await connection.ensure_schema(conn)
                    assert not await conn.fetchval("SELECT convalidated FROM pg_constraint "
                        "WHERE conrelid='agent_runtimes'::regclass AND conname='agent_runtimes_capabilities_check'")
                await connection.ensure_schema(conn)
            await conn.execute("UPDATE agent_runtimes SET capabilities=ARRAY['mid_turn_input'] WHERE id='rt-claude-code'")
            with pytest.raises(asyncpg.CheckViolationError):
                await conn.execute("UPDATE agent_runtimes SET capabilities=ARRAY['unknown'] WHERE id='rt-claude-code'")
            assert await conn.fetchval("SELECT convalidated FROM pg_constraint "
                "WHERE conrelid='agent_runtimes'::regclass AND conname='agent_runtimes_capabilities_check'")
            assert not await connection.schema_change_pending(conn)
            before = await conn.fetch("SELECT name FROM schema_migrations ORDER BY name")
            await connection.ensure_schema(conn)
            assert await conn.fetch("SELECT name FROM schema_migrations ORDER BY name") == before
    asyncio.run(asyncio.wait_for(test(), 30))
