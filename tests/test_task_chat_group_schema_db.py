"""The task's ``chat_group`` column: the upgrade of a database without it keeps its tasks, adds the column false, and
matches a fresh database."""

import asyncio

import pytest

from looper.db import connection
from tests.bridge_schema_support import structure, witness
from tests.db_support import DSN, fresh_connection

pytestmark = pytest.mark.skipif(not DSN, reason="requires disposable LOOPER_TEST_DATABASE_URL")


def test_chat_group_upgrade_preserves_tasks_and_matches_fresh():
    async def test():
        async with fresh_connection() as old, fresh_connection() as fresh:
            await connection.ensure_schema(old)
            await old.execute("ALTER TABLE tasks DROP COLUMN chat_group")
            await old.execute("DELETE FROM schema_migrations WHERE name='task_chat_group'")
            await old.execute("INSERT INTO projects (id,name) VALUES ('p','Example')")
            await old.execute("INSERT INTO tasks (id,requirement,project_id,chat_id,chat_kind) "
                              "VALUES ('t','r','p','oc_1','chat')")
            before, shape = await witness(old), await structure(old)
            assert await connection.schema_change_pending(old)
            assert await witness(old) == before and await structure(old) == shape
            await connection.ensure_schema(old)
            assert dict(await old.fetchrow("SELECT * FROM tasks")) == {**before["tasks"][0], "chat_group": False}
            await connection.ensure_schema(fresh)
            assert await structure(old) == await structure(fresh)
            after, shape = await witness(old), await structure(old)
            assert not await connection.schema_change_pending(old)
            await connection.ensure_schema(old)
            assert await witness(old) == after and await structure(old) == shape
            assert await old.fetchval("SELECT count(*) FROM schema_migrations WHERE name='task_chat_group'") == 1
    asyncio.run(asyncio.wait_for(test(), 30))
