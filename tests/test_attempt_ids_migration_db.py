"""Fresh and upgraded schemas install observation columns without backfilling."""

import asyncio

import pytest

from looper.db import connection
from looper.db.migrations import attempt_ids, step_attempts, task_outcomes
from tests.db_support import DSN, fresh_connection

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")


def test_attempt_migrations_are_idempotent_and_online_constraints_are_valid():
    async def test():
        async with fresh_connection() as conn:
            await connection.ensure_schema(conn)
            assert await conn.fetchval("SELECT array_agg(name) FROM schema_migrations") == ['post_bridge_baseline']
            await connection.ensure_schema(conn)
            for module in (step_attempts, task_outcomes, attempt_ids):
                assert not await module.pending(conn)
            rows = await conn.fetch("SELECT conname,convalidated FROM pg_constraint WHERE conrelid IN "
                                    "('llm_records'::regclass,'advisor_advice'::regclass) "
                                    "AND conname IN ('llm_records_attempt_id_fkey','advisor_advice_attempt_id_fkey')")
            assert len(rows) == 2 and all(row['convalidated'] for row in rows)
            assert await conn.fetchval("SELECT indisvalid FROM pg_index WHERE indexrelid='idx_llm_records_attempt'::regclass")
            assert 'WHERE (attempt_id IS NOT NULL)' in await conn.fetchval(
                "SELECT indexdef FROM pg_indexes WHERE schemaname=current_schema() AND indexname='idx_llm_records_attempt'")

    asyncio.run(test())


def test_upgrade_keeps_existing_rows_null_and_does_not_backfill_attempts():
    async def test():
        async with fresh_connection() as conn:
            await connection.ensure_schema(conn)
            await conn.execute("INSERT INTO projects(id,name) VALUES ('p','Synthetic project')")
            await conn.execute("INSERT INTO tasks(id,requirement,project_id) VALUES ('t','Synthetic task','p')")
            await conn.execute("INSERT INTO llm_records(agent_id,task_id) VALUES ('worker-old','t')")
            await conn.execute('DROP TABLE task_outcomes; DROP TABLE step_attempts CASCADE; '
                               'ALTER TABLE llm_records DROP COLUMN attempt_id; ALTER TABLE tasks DROP COLUMN run_no')
            await conn.execute("DELETE FROM schema_migrations WHERE name=ANY($1::text[])", [
                'step_attempts', 'task_outcomes', 'attempt_ids', 'attempt_ids_online'])
            assert await connection.schema_change_pending(conn)
            for module in (step_attempts, task_outcomes, attempt_ids):
                assert await module.pending(conn)
                await module.apply(conn, sql='', state='')
            assert not await conn.fetchval(
                "SELECT bool_or(convalidated) FROM pg_constraint "
                "WHERE conname IN ('llm_records_attempt_id_fkey','advisor_advice_attempt_id_fkey') "
                "AND connamespace=current_schema()::regnamespace")
            assert await conn.fetchval("SELECT to_regclass('idx_llm_records_attempt')") is None
            await connection.ensure_schema(conn)
            assert await conn.fetchval(
                "SELECT bool_and(convalidated) FROM pg_constraint "
                "WHERE conname IN ('llm_records_attempt_id_fkey','advisor_advice_attempt_id_fkey') "
                "AND connamespace=current_schema()::regnamespace")
            assert await conn.fetchval("SELECT indisvalid FROM pg_index WHERE indexrelid='idx_llm_records_attempt'::regclass")
            assert await conn.fetchval('SELECT attempt_id FROM llm_records') is None
            assert await conn.fetchval('SELECT run_no FROM tasks') == 1
            assert await conn.fetchval('SELECT count(*) FROM step_attempts') == 0
            assert await conn.fetchval('SELECT count(*) FROM task_outcomes') == 0

    asyncio.run(test())


@pytest.mark.parametrize('changes', [
    "attempt=0", "task_run_no=0", "worker_runs=-1", "review_runs=-1", "blocking_findings=-1",
    "trigger='unknown'", "run_outcome='run_failed'", "review_outcome='unknown'", "decision='retry'",
    "decided_at=now()", "decision='accept',decided_at=now(),failure_kind='contract'",
])
def test_attempt_constraints_reject_inconsistent_observations(changes):
    import asyncpg

    async def test():
        async with fresh_connection() as conn:
            await connection.ensure_schema(conn)
            await conn.execute("INSERT INTO projects(id,name) VALUES ('p','Synthetic project')")
            await conn.execute("INSERT INTO tasks(id,requirement,project_id) VALUES ('t','Synthetic task','p')")
            await conn.execute("INSERT INTO step_attempts(task_id,step_key,task_run_no,attempt,trigger) "
                               "VALUES ('t','s1',1,1,'first')")
            with pytest.raises(asyncpg.CheckViolationError):
                await conn.execute('UPDATE step_attempts SET '+changes)

    asyncio.run(test())
