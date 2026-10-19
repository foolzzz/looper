"""The read_tools capability constraint on fresh, old and already upgraded PostgreSQL schemas.

Runs only when LOOPER_TEST_DATABASE_URL points at a disposable database, in a
new schema of its own, dropped afterwards.
"""

from __future__ import annotations

import asyncio

import pytest

from tests.test_catalog_db import DSN, _fresh_schema

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")

_OLD_CONSTRAINT = """
ALTER TABLE agent_runtimes DROP CONSTRAINT agent_runtimes_capabilities_check;
ALTER TABLE agent_runtimes ADD CONSTRAINT agent_runtimes_capabilities_check CHECK (capabilities <@ ARRAY[
    'run', 'resident_session', 'resume', 'structured_output', 'claude_plugins', 'mcp', 'add_dirs',
    'compaction_events', 'autonomous_turns', 'cost_report']);
"""

_INSERT = "INSERT INTO agent_runtimes (id, name, type, capabilities) VALUES ($1, $1, 'omp', $2::text[])"


def test_a_new_database_accepts_read_tools() -> None:
    from looper.db import connection

    async def _main() -> None:
        async with _fresh_schema() as pool:
            await connection.ensure_schema(pool)
            try:
                await pool.execute(_INSERT, "rt-read-tools", ["read_tools"])
                assert await pool.fetchval("SELECT capabilities FROM agent_runtimes WHERE id = 'rt-read-tools'") == [
                    "read_tools"]
            finally:
                await pool.execute("DELETE FROM agent_runtimes WHERE id = 'rt-read-tools'")

    asyncio.run(_main())


def test_an_old_constraint_is_upgraded_and_still_rejects_unknown_capabilities() -> None:
    import asyncpg

    from looper.db import connection

    async def _main() -> None:
        async with _fresh_schema() as pool:
            await connection.ensure_schema(pool)
            await pool.execute(_OLD_CONSTRAINT)
            try:
                with pytest.raises(asyncpg.CheckViolationError) as rejected:
                    await pool.execute(_INSERT, "rt-read-tools", ["read_tools"])
                assert rejected.value.constraint_name == "agent_runtimes_capabilities_check"

                await connection.ensure_schema(pool)
                await pool.execute(_INSERT, "rt-read-tools", ["read_tools"])
                assert await pool.fetchval("SELECT capabilities FROM agent_runtimes WHERE id = 'rt-read-tools'") == [
                    "read_tools"]
                for capabilities in (["unknown"], ["read_tools", "unknown"]):
                    with pytest.raises(asyncpg.CheckViolationError) as rejected:
                        await pool.execute(_INSERT, "rt-invalid", capabilities)
                    assert rejected.value.constraint_name == "agent_runtimes_capabilities_check"
            finally:
                await pool.execute("DELETE FROM agent_runtimes WHERE id IN ('rt-read-tools', 'rt-invalid')")

    asyncio.run(_main())


def test_repeated_schema_setup_keeps_one_constraint_and_preserves_runtime_data() -> None:
    from looper.db import connection

    async def _main() -> None:
        async with _fresh_schema() as pool:
            await connection.ensure_schema(pool)
            try:
                await pool.execute(_INSERT, "rt-read-tools", ["read_tools"])
                before = await pool.fetchrow("SELECT * FROM agent_runtimes WHERE id = 'rt-read-tools'")
                for _ in range(2):
                    await connection.ensure_schema(pool)
                    constraints = await pool.fetch(
                        "SELECT pg_get_constraintdef(oid) AS definition FROM pg_constraint "
                        "WHERE conrelid = to_regclass('agent_runtimes') "
                        "AND conname = 'agent_runtimes_capabilities_check'")
                    assert len(constraints) == 1 and "read_tools" in constraints[0]["definition"]
                    assert await pool.fetchrow("SELECT * FROM agent_runtimes WHERE id = 'rt-read-tools'") == before
            finally:
                await pool.execute("DELETE FROM agent_runtimes WHERE id = 'rt-read-tools'")

    asyncio.run(_main())
