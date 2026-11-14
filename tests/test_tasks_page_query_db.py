"""Task list query plans and additive index upgrades on disposable PostgreSQL."""

import asyncio
import json

import pytest

from tests.db_support import DSN, fresh_connection, run_db

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")


def test_default_task_list_uses_creation_order_index():
    from looper.db import tasks

    async def run(pool):
        await pool.execute("INSERT INTO projects (id, name) VALUES ('demo', 'Demo')")
        await pool.execute("INSERT INTO tasks (id, requirement, project_id, created_at) "
                           "SELECT 'task-' || lpad(n::text, 4, '0'), 'Synthetic task', 'demo', "
                           "now() - n * interval '1 minute' FROM generate_series(1, 1000) n")
        await pool.execute("ANALYZE tasks; ANALYZE projects; ANALYZE schedules")
        captured = []

        class RecordingPool:
            async def fetch(self, query, *args):
                captured.append((query, args))
                return await pool.fetch(query, *args)

        rows = await tasks.list_tasks(limit=51, pool=RecordingPool())
        assert len(rows) == 51 and rows[0]["id"] == "task-0001"
        assert not {"config", "process_record", "origin_reply_route"} & rows[0].keys()
        assert len(captured) == 1
        query, args = captured[0]
        plan = json.loads(await pool.fetchval("EXPLAIN (FORMAT JSON) " + query, *args))[0]["Plan"]

        def nodes(node):
            yield node
            for child in node.get("Plans", []):
                yield from nodes(child)

        assert any(node["Node Type"] == "Index Scan" and node.get("Index Name") == "idx_tasks_created"
                   for node in nodes(plan)), plan

    # Includes cold schema creation; EXPLAIN asserts the plan rather than wall time.
    run_db(run, timeout=120)


@pytest.mark.parametrize("upgrade", [False, True])
def test_task_list_index_installs_on_fresh_and_existing_databases(upgrade):
    from looper.db import connection

    async def run():
        async with fresh_connection(command_timeout=120) as conn:
            await connection.ensure_schema(conn)
            if upgrade:
                await conn.execute("DROP INDEX idx_tasks_created")
                await conn.execute("DELETE FROM schema_migrations WHERE name = 'tasks_created_index'")
                await conn.execute("INSERT INTO projects (id, name) VALUES ('demo', 'Demo')")
                await conn.execute("INSERT INTO tasks (id, requirement, project_id) VALUES ('task-a', 'Keep', 'demo')")
                assert await connection.schema_change_pending(conn)
                await connection.ensure_schema(conn)
                assert await conn.fetchval("SELECT requirement FROM tasks WHERE id = 'task-a'") == "Keep"
            definition = await conn.fetchval("SELECT pg_get_indexdef(to_regclass('idx_tasks_created'))")
            assert "(created_at DESC, id DESC)" in definition
            assert not await connection.schema_change_pending(conn)
            await connection.ensure_schema(conn)
            assert await conn.fetchval("SELECT pg_get_indexdef(to_regclass('idx_tasks_created'))") == definition

    asyncio.run(asyncio.wait_for(run(), 120))
