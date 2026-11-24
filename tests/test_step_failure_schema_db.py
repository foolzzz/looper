"""The named failure-handling migration repairs objects and preserves old rows."""
import pytest
import asyncpg

from tests.db_support import DSN, run_db

pytestmark = pytest.mark.skipif(not DSN, reason='requires disposable PostgreSQL')


def test_fresh_upgrade_partial_restore_and_online_validation(monkeypatch):
    from looper.db import connection, bridge_upgrade
    from looper.db.migrations import step_failure_handling as migration

    async def body(pool):
        assert {row['name'] for row in await pool.fetch('SELECT name FROM schema_migrations')} == {bridge_upgrade.BASELINE}
        await pool.execute("INSERT INTO projects(id,name) VALUES ('schema-project','Schema checks')")
        await pool.execute("INSERT INTO tasks(id,project_id,requirement) VALUES ('schema-task','schema-project','Kept task')")
        await pool.execute("INSERT INTO task_journal(task_id,step_id,kind,summary) VALUES ('schema-task','','decision','Kept journal')")
        await pool.execute('DROP TABLE step_holds,step_failure_releases,step_failures,step_failure_runs,step_failure_state,task_quota_waits')
        await pool.execute("INSERT INTO schema_migrations(name) VALUES('step_failure_handling')")
        async with pool.acquire() as conn:
            assert await migration.pending(conn)
        await connection.ensure_schema(pool)
        assert await pool.fetchval("SELECT requirement FROM tasks WHERE id='schema-task'") == 'Kept task'
        assert await pool.fetchval("SELECT summary FROM task_journal WHERE task_id='schema-task'") == 'Kept journal'
        assert await pool.fetchval("SELECT convalidated FROM pg_constraint WHERE conname='tasks_fail_kind_check' AND conrelid='tasks'::regclass")
        assert await pool.fetchval("SELECT convalidated FROM pg_constraint WHERE conname='task_journal_kind_check' AND conrelid='task_journal'::regclass")
        await pool.execute('ALTER TABLE step_holds DROP COLUMN reminded_at, DROP CONSTRAINT step_holds_until_check; '
                           'ALTER TABLE step_failures DROP CONSTRAINT step_failures_record_id_fkey; '
                           'ALTER TABLE step_failure_state DROP CONSTRAINT step_failure_state_pkey')
        async with pool.acquire() as conn:
            assert await migration.pending(conn)
        await connection.ensure_schema(pool)
        async with pool.acquire() as conn:
            assert not await migration.pending(conn)
        await connection.ensure_schema(pool)
        assert await pool.fetchval("SELECT count(*) FROM pg_constraint WHERE conname IN ('step_failures_record_id_fkey','step_failure_state_pkey') "
                                   "AND conrelid IN ('step_failures'::regclass,'step_failure_state'::regclass)") == 2
        for bad in (('quota','login'), ('transient','usage_limit'), ('environment','unknown')):
            with pytest.raises(asyncpg.CheckViolationError):
                await pool.execute("INSERT INTO step_failures(task_id,step_identity,step_key,run_id,role,failure_class,failure_cause,streak,action) "
                    "VALUES('schema-task','gone','work','bad','worker',$1,$2,1,'planner')", *bad)
        await pool.execute("INSERT INTO task_steps(id,task_id,step_index,instruction) VALUES('schema-step','schema-task',0,'Work')")
        failure_id = await pool.fetchval("INSERT INTO step_failures(task_id,step_id,step_identity,step_key,run_id,role,failure_class,failure_cause,streak,action) "
            "VALUES('schema-task','schema-step','schema-step','work','valid','worker','quota','rate_limit',1,'hold') RETURNING id")
        for kind, cause, until in (('quota','rate_limit',None), ('quota','login','now'), ('environment','login','now')):
            with pytest.raises(asyncpg.CheckViolationError):
                await pool.execute("INSERT INTO step_holds(step_id,task_id,kind,cause,until,failure_id) "
                    "VALUES('schema-step','schema-task',$1,$2,CASE WHEN $3::text IS NULL THEN NULL ELSE now() END,$4)",
                    kind, cause, until, failure_id)

    run_db(body, monkeypatch, timeout=60)


def test_failure_handling_uses_reserved_order():
    from looper.db.migrations import discover
    migrations = discover()
    chosen = [item for item in migrations if item.name == 'step_failure_handling']
    assert len(chosen) == 1
    assert chosen[0].order == chosen[0].pending_order == chosen[0].online_order == 1920
    assert sum(item.order == 1920 for item in migrations) == 1
