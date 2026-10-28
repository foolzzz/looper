"""Current per-task concurrency constraints on disposable PostgreSQL."""

from __future__ import annotations

import asyncio

import pytest

from tests.db_support import DSN, fresh_schema

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")


def test_a_new_database_has_the_narrow_constraint() -> None:
    import asyncpg

    from looper.db import connection

    async def _main() -> None:
        async with fresh_schema() as pool:
            await connection.ensure_schema(pool)
            await pool.execute("INSERT INTO agent_setting_layers (scope, role, max_concurrency) VALUES "
                               "('role', 'reviewer', 2)")
            await pool.execute("INSERT INTO projects (id, name) VALUES ('proj-a', 'a')")
            with pytest.raises(asyncpg.CheckViolationError):
                await pool.execute("INSERT INTO agent_setting_layers (scope, role, project_id, max_concurrency) "
                                   "VALUES ('role_project', 'reviewer', 'proj-a', 2)")

    asyncio.run(_main())
