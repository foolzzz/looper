"""The task_feedback migration on a fresh and an upgraded schema."""

import asyncio

import pytest

from looper.db import connection
from looper.db.migrations import task_feedback
from tests.db_support import DSN, fresh_connection

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")


def test_fresh_schema_has_the_table_and_only_the_baseline_marker():
    async def test():
        async with fresh_connection() as conn:
            await connection.ensure_schema(conn)
            assert await conn.fetchval("SELECT array_agg(name) FROM schema_migrations") == ['post_bridge_baseline']
            await connection.ensure_schema(conn)
            assert not await task_feedback.pending(conn)
            await conn.execute("INSERT INTO projects(id,name) VALUES ('p','Synthetic project')")
            await conn.execute("INSERT INTO tasks(id,requirement,project_id) VALUES ('t','Synthetic task','p')")
            with pytest.raises(Exception):
                await conn.execute("INSERT INTO task_feedback(task_id,verdict) VALUES ('t','bad')")
            with pytest.raises(Exception):
                await conn.execute("INSERT INTO task_feedback(task_id,verdict,note) VALUES ('t','defect',repeat('x',501))")
            await conn.execute("INSERT INTO task_feedback(task_id,verdict,note) VALUES ('t','defect',repeat('x',500))")
            await conn.execute("DELETE FROM tasks WHERE id='t'")
            assert await conn.fetchval("SELECT count(*) FROM task_feedback") == 0

    asyncio.run(test())


def test_an_upgrade_adds_the_table_once_and_checks_the_object_not_only_the_marker():
    async def test():
        async with fresh_connection() as conn:
            await connection.ensure_schema(conn)
            await conn.execute("DROP TABLE task_feedback")
            assert await task_feedback.pending(conn) and await connection.schema_change_pending(conn)
            await task_feedback.apply(conn, sql='', state='')
            assert not await task_feedback.pending(conn)
            assert await conn.fetchval("SELECT count(*) FROM schema_migrations WHERE name='task_feedback'") == 1
            await task_feedback.apply(conn, sql='', state='')
            assert await conn.fetchval("SELECT count(*) FROM schema_migrations WHERE name='task_feedback'") == 1
            # A marker with the table present is done; a table that exists without the marker is not re-created.
            await conn.execute("DELETE FROM schema_migrations WHERE name='task_feedback'")
            assert not await task_feedback.pending(conn)

    asyncio.run(test())
