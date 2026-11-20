"""Online migration serialization on independent PostgreSQL sessions."""
import asyncio
from contextlib import asynccontextmanager

import asyncpg
import pytest

from looper.db import connection
from tests.db_support import DSN, fresh_schema

pytestmark = pytest.mark.skipif(not DSN, reason="requires disposable LOOPER_TEST_DATABASE_URL")


@asynccontextmanager
async def pending_online():
    async with fresh_schema(max_size=4) as pool:
        await pool.execute("""
            CREATE TABLE schema_migrations (name TEXT PRIMARY KEY);
            CREATE TABLE llm_records (id BIGSERIAL PRIMARY KEY);
            CREATE TABLE task_steps (difficulty TEXT);
            ALTER TABLE task_steps ADD CONSTRAINT task_steps_difficulty_check
                CHECK (difficulty IN ('routine', 'hard', 'critical')) NOT VALID;
            INSERT INTO schema_migrations VALUES ('run_tree'), ('step_difficulty');
        """)
        for sql in connection._run_tree_ddl():
            await pool.execute(sql)
        yield pool


async def wait_for_query(observer, conn, prefix):
    async with asyncio.timeout(5):
        while not await observer.fetchval(
            "SELECT wait_event_type = 'Lock' AND query LIKE $2 FROM pg_stat_activity WHERE pid=$1",
            conn.get_server_pid(), prefix + "%",
        ):
            await asyncio.sleep(0.01)


async def assert_finished(conn):
    markers = await conn.fetch("SELECT name FROM schema_migrations WHERE name LIKE '%_online' ORDER BY name")
    assert [r["name"] for r in markers] == ["run_tree_online", "step_difficulty_online"]
    assert await conn.fetchval("SELECT bool_and(convalidated) FROM pg_constraint "
                               "WHERE conrelid IN ('llm_records'::regclass, 'task_steps'::regclass)")
    assert await conn.fetchval("SELECT indisvalid FROM pg_index "
                               "WHERE indexrelid='idx_llm_records_parent'::regclass")


def test_concurrent_online_steps_finish_once_without_a_validate_index_deadlock():
    async def test():
        async with pending_online() as pool, pool.acquire() as first, pool.acquire() as second, \
                pool.acquire() as writer, pool.acquire() as observer:
            queries = []
            first.add_query_logger(lambda q: queries.append(q.query))
            second.add_query_logger(lambda q: queries.append(q.query))
            # Keep CIC in its writer-wait phase after it has the table lock. The
            # second validator then waits for that lock with a virtual transaction
            # CIC must wait for: releasing this writer reproduces the old cycle.
            tx = writer.transaction()
            await tx.start()
            await writer.execute("INSERT INTO llm_records DEFAULT VALUES")
            runs = [asyncio.create_task(connection._finish_online(first))]
            try:
                await wait_for_query(observer, first, "CREATE INDEX CONCURRENTLY")
                runs.append(asyncio.create_task(connection._finish_online(second)))
                async with asyncio.timeout(5):
                    while True:
                        state = await observer.fetchrow(
                            "SELECT query, wait_event_type, xact_start FROM pg_stat_activity WHERE pid=$1",
                            second.get_server_pid())
                        if state["wait_event_type"] == "Lock" or (
                            "advisory" in state["query"] and state["xact_start"] is None
                        ):
                            break
                        await asyncio.sleep(0.01)
                await tx.commit()
                await asyncio.wait_for(asyncio.gather(*runs), 10)
                await assert_finished(observer)
                for step in connection._ONLINE_STEPS:
                    for table, constraint in step.validate:
                        assert queries.count(f"ALTER TABLE {table} VALIDATE CONSTRAINT {constraint}") == 1
                assert sum(q.startswith("INSERT INTO schema_migrations") for q in queries) == 2
                assert sum(q.startswith("CREATE INDEX CONCURRENTLY") for q in queries) == 1
                assert not await observer.fetchval(
                    "SELECT EXISTS (SELECT 1 FROM pg_locks WHERE locktype='advisory' AND pid=ANY($1))",
                    [first.get_server_pid(), second.get_server_pid()])
            finally:
                if writer.is_in_transaction():
                    await tx.rollback()
                for task in runs:
                    task.cancel()
                await asyncio.gather(*runs, return_exceptions=True)
    asyncio.run(test())


def test_online_lock_timeout_defers_and_the_next_start_finishes(monkeypatch, caplog):
    async def test():
        async with pending_online() as pool, pool.acquire() as owner, pool.acquire() as waiter:
            monkeypatch.setattr(connection, "_ONLINE_LOCK_WAIT_SECONDS", 0.1)
            await owner.execute("SELECT pg_advisory_lock($1)", connection._ONLINE_LOCK)
            try:
                await asyncio.wait_for(connection._finish_online(waiter), 1)
                assert not waiter.is_in_transaction()
                assert not await waiter.fetchval("SELECT EXISTS (SELECT 1 FROM schema_migrations "
                                                  "WHERE name LIKE '%_online')")
                assert not await waiter.fetchval("SELECT convalidated FROM pg_constraint "
                    "WHERE conrelid='llm_records'::regclass AND conname='llm_records_run_kind_check'")
                assert any(r.levelname == "WARNING" and "online" in r.message for r in caplog.records)
            finally:
                await owner.execute("SELECT pg_advisory_unlock($1)", connection._ONLINE_LOCK)
            await connection._finish_online(waiter)
            await assert_finished(waiter)
    asyncio.run(test())


@pytest.mark.parametrize("cancel", [False, True], ids=["read-only-failure", "cancelled-index"])
def test_interrupted_online_step_releases_the_lock_and_resumes(cancel):
    async def test():
        async with pending_online() as pool, pool.acquire() as conn, pool.acquire() as other:
            if cancel:
                tx = other.transaction()
                await tx.start()
                await other.execute("INSERT INTO llm_records DEFAULT VALUES")
                task = asyncio.create_task(connection._finish_online(conn))
                try:
                    await wait_for_query(pool, conn, "CREATE INDEX CONCURRENTLY")
                    task.cancel()
                    with pytest.raises(asyncio.CancelledError):
                        await task
                finally:
                    await tx.rollback()
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
                assert not await conn.fetchval("SELECT indisvalid FROM pg_index "
                    "WHERE indexrelid='idx_llm_records_parent'::regclass")
            else:
                # A failed validation must not mark the step done.
                await conn.execute("SET default_transaction_read_only=on")
                try:
                    with pytest.raises(asyncpg.ReadOnlySQLTransactionError):
                        await connection._finish_online(conn)
                finally:
                    await conn.execute("SET default_transaction_read_only=off")
            assert not await conn.fetchval("SELECT EXISTS (SELECT 1 FROM schema_migrations "
                                           "WHERE name='run_tree_online')")
            assert await other.fetchval("SELECT pg_try_advisory_lock($1)", connection._ONLINE_LOCK)
            await other.execute("SELECT pg_advisory_unlock($1)", connection._ONLINE_LOCK)
            await connection._finish_online(other)
            await assert_finished(other)
    asyncio.run(test())


def test_online_steps_defer_in_a_callers_transaction(caplog):
    async def test():
        async with pending_online() as pool, pool.acquire() as conn:
            async with conn.transaction():
                await connection._finish_online(conn)
                assert not await conn.fetchval("SELECT EXISTS (SELECT 1 FROM schema_migrations "
                                               "WHERE name LIKE '%_online')")
            assert any("online" in r.message for r in caplog.records)
            await connection._finish_online(conn)
            await assert_finished(conn)
    asyncio.run(test())
