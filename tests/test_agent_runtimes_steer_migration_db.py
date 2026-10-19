"""The steer capability constraint on fresh, old and already upgraded PostgreSQL schemas.

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

_READ_TOOLS_CONSTRAINT = _OLD_CONSTRAINT.replace("'cost_report'", "'cost_report', 'read_tools'")

_INSERT = "INSERT INTO agent_runtimes (id, name, type, capabilities) VALUES ($1, $1, 'omp', $2::text[])"


def test_a_new_database_accepts_steer() -> None:
    from looper.db import connection

    async def _main() -> None:
        async with _fresh_schema() as pool:
            await connection.ensure_schema(pool)
            try:
                await pool.execute(_INSERT, "rt-steer", ["steer"])
                assert await pool.fetchval("SELECT capabilities FROM agent_runtimes WHERE id = 'rt-steer'") == [
                    "steer"]
            finally:
                await pool.execute("DELETE FROM agent_runtimes WHERE id = 'rt-steer'")

    asyncio.run(_main())


@pytest.mark.parametrize("constraint", [_OLD_CONSTRAINT, _READ_TOOLS_CONSTRAINT], ids=["old", "read-tools"])
def test_an_old_constraint_is_upgraded_and_still_rejects_unknown_capabilities(constraint: str) -> None:
    import asyncpg

    from looper.db import connection

    async def _main() -> None:
        async with _fresh_schema() as pool:
            await connection.ensure_schema(pool)
            await pool.execute(constraint)
            try:
                with pytest.raises(asyncpg.CheckViolationError) as rejected:
                    await pool.execute(_INSERT, "rt-steer", ["steer"])
                assert rejected.value.constraint_name == "agent_runtimes_capabilities_check"

                await connection.ensure_schema(pool)
                await pool.execute(_INSERT, "rt-steer", ["steer"])
                assert await pool.fetchval("SELECT capabilities FROM agent_runtimes WHERE id = 'rt-steer'") == [
                    "steer"]
                await pool.execute(_INSERT, "rt-read-tools", ["read_tools", "steer"])
                for capabilities in (["unknown"], ["steer", "unknown"]):
                    with pytest.raises(asyncpg.CheckViolationError) as rejected:
                        await pool.execute(_INSERT, "rt-invalid", capabilities)
                    assert rejected.value.constraint_name == "agent_runtimes_capabilities_check"
            finally:
                await pool.execute("DELETE FROM agent_runtimes WHERE id IN ('rt-steer', 'rt-read-tools', 'rt-invalid')")

    asyncio.run(_main())


def test_repeated_schema_setup_keeps_one_constraint_and_preserves_runtime_data() -> None:
    from looper.db import connection

    async def _main() -> None:
        async with _fresh_schema() as pool:
            await connection.ensure_schema(pool)
            try:
                await pool.execute(_INSERT, "rt-steer", ["steer"])
                before = await pool.fetchrow("SELECT * FROM agent_runtimes WHERE id = 'rt-steer'")
                for _ in range(2):
                    await connection.ensure_schema(pool)
                    constraints = await pool.fetch(
                        "SELECT pg_get_constraintdef(oid) AS definition FROM pg_constraint "
                        "WHERE conrelid = to_regclass('agent_runtimes') "
                        "AND conname = 'agent_runtimes_capabilities_check'")
                    assert len(constraints) == 1 and "steer" in constraints[0]["definition"]
                    assert await pool.fetchrow("SELECT * FROM agent_runtimes WHERE id = 'rt-steer'") == before
            finally:
                await pool.execute("DELETE FROM agent_runtimes WHERE id = 'rt-steer'")

    asyncio.run(_main())
