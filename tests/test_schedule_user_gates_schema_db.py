"""The opt-in column exists on fresh, bridge and existing final baselines."""

import asyncio

import asyncpg
import pytest

from looper.db import connection
from tests.bridge_schema_support import structure
from tests.db_support import DSN, fresh_connection

pytestmark = pytest.mark.skipif(not DSN, reason="requires disposable LOOPER_TEST_DATABASE_URL")


def test_existing_final_baseline_upgrades_once_and_preserves_schedule_choices():
    async def test():
        async with fresh_connection() as old, fresh_connection() as fresh:
            # Reproduce the released final baseline, before the additive column.
            column = "    user_gates TEXT NOT NULL DEFAULT 'off' CHECK (user_gates IN ('off', 'on')),\n"
            sql = connection.SCHEMA_PATH.read_text()
            assert sql.count(column) == 1
            await old.execute(sql.replace(column, ""))
            await old.execute("INSERT INTO schema_migrations (name) VALUES ('post_bridge_baseline')")
            await old.execute("INSERT INTO registry_schema_version (id, version) VALUES (TRUE, 4)")
            await old.execute(connection.SEEDS_PATH.read_text())
            await old.execute("INSERT INTO projects (id,name) VALUES ('p','Example')")
            await old.execute("INSERT INTO schedules (id,project_id,name,cron,timezone,requirement,notify) "
                              "VALUES ('s','p','Reports','0 * * * *','UTC','Report','never')")
            before = dict(await old.fetchrow("SELECT * FROM schedules"))
            assert await connection.schema_change_pending(old)
            assert dict(await old.fetchrow("SELECT * FROM schedules")) == before
            await connection.ensure_schema(old)
            assert dict(await old.fetchrow("SELECT * FROM schedules")) == {**before, "user_gates": "off"}
            await connection.ensure_schema(fresh)
            assert await structure(old) == await structure(fresh)
            await old.execute("UPDATE schedules SET user_gates='on'")
            assert not await connection.schema_change_pending(old)
            await connection.ensure_schema(old)
            assert await old.fetchval("SELECT user_gates FROM schedules") == "on"
            for value in (None, "yes"):
                with pytest.raises((asyncpg.NotNullViolationError, asyncpg.CheckViolationError)):
                    await old.execute("UPDATE schedules SET user_gates=$1", value)
    asyncio.run(asyncio.wait_for(test(), 30))
