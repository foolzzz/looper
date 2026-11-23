"""Explorer persistence, atomic reservations and independent cost accounting."""
from __future__ import annotations

import asyncio

import asyncpg
import pytest

from looper.db import agent_spawns, connection, llm_records
from looper.db.agent_spawns import SpawnState as S, TransitionError
from tests.db_support import DSN, fresh_connection, run_db

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")


async def _task(pool):
    await pool.execute("INSERT INTO projects(id, name) VALUES ('project-explore', 'Explore')")
    await pool.execute("INSERT INTO tasks(id, project_id, requirement, status) "
                       "VALUES ('task-explore', 'project-explore', 'Inspect code', 'running')")
    return await pool.fetchval("INSERT INTO llm_records(agent_id,task_id,status,run_kind) "
                              "VALUES ('worker-explore','task-explore','running','worker') RETURNING id")


async def _spawn(pool, parent, key='one'):
    return await agent_spawns.create(kind='explorer', task_id='task-explore', step_id=None,
        parent_record_id=parent, brief={'goal': 'Inspect'}, inputs={}, budget_usd=.5,
        state=S.QUEUED, idempotency_key=key, spawner='worker@s1', profile_id='chosen',
        delivery='tool', parent_spend_known=False, pool=pool)


def test_migration_constraints_and_unique_key():
    async def test(pool):
        parent = await _task(pool)
        row = await _spawn(pool, parent)
        assert row['state'] == 'queued' and row['parent_spend_known'] is False
        assert await agent_spawns.by_key(parent, 'one', pool=pool) == row
        with pytest.raises(asyncpg.UniqueViolationError):
            await _spawn(pool, parent)
        for sql in (
            "UPDATE agent_spawns SET state='cancelled', finished_at=now()",
            "UPDATE agent_spawns SET cancel_reason='operator'",
            "UPDATE agent_spawns SET failure_class='quota',failure_cause='profile'",
            "UPDATE agent_spawns SET attempt=3",
        ):
            with pytest.raises(asyncpg.CheckViolationError):
                await pool.execute(sql)
        assert await pool.fetchval("SELECT indisunique AND indisvalid FROM pg_index "
                                   "WHERE indexrelid=to_regclass('idx_agent_spawns_key')")
        await connection.ensure_schema(pool)
        assert (await agent_spawns.get(row['id'], pool=pool))['id'] == row['id']
    run_db(test)


def test_cancel_and_finish_compare_and_swap_and_release():
    async def test(pool):
        parent = await _task(pool)
        row = await _spawn(pool, parent)
        assert (await agent_spawns.commitment('task-explore', parent, pool=pool))['task_reserved'] == .5
        row = await agent_spawns.move(row['id'], S.ASKING, expect=S.QUEUED,
            child_process={'pid': 123, 'started_at': 1.2}, pool=pool)
        results = await asyncio.gather(
            agent_spawns.move(row['id'], S.FINISHING, expect=S.ASKING, pool=pool),
            agent_spawns.move(row['id'], S.CANCELLING, expect=S.ASKING,
                              cancel_reason='requested', pool=pool))
        assert sum(r is not None for r in results) == 1
        winner = next(r for r in results if r)
        state = S(winner['state'])
        terminal = S.CANCELLED if state is S.CANCELLING else S.ANSWERED
        await agent_spawns.move(row['id'], terminal, expect=state, spent_usd=.02, pool=pool)
        usage = await agent_spawns.commitment('task-explore', parent, pool=pool)
        assert usage['task_reserved'] == 0 and usage['task_committed'] == .02
        with pytest.raises(TransitionError):
            await agent_spawns.move(row['id'], S.ASKING, expect=terminal, pool=pool)
    run_db(test)


def test_task_lock_serializes_two_connections():
    async def test(pool):
        parent = await _task(pool)
        async def reserve(key):
            async with agent_spawns.task_lock('task-explore', pool=pool) as conn:
                usage = await agent_spawns.commitment('task-explore', parent, pool=conn)
                if usage['task_committed'] >= .5:
                    return None
                return await _spawn(conn, parent, key)
        rows = await asyncio.gather(reserve('one'), reserve('two'))
        assert sum(row is not None for row in rows) == 1
    run_db(test)


def test_cost_shape_off_and_explorer_share_when_on():
    async def test(pool):
        await _task(pool)
        before = (await llm_records.task_costs(['task-explore'], pool=pool))['task-explore']
        assert set(before['by_role']) == {'planner', 'worker', 'reviewer'}
        await pool.execute("UPDATE projects SET subagents='on'")
        await pool.execute("INSERT INTO llm_records(agent_id,task_id,status,run_kind,cost_usd) "
                           "VALUES ('explorer-one','task-explore','success','explorer',.125)")
        after = (await llm_records.task_costs(['task-explore'], pool=pool))['task-explore']
        assert after['by_role']['explorer'] == {'cost_usd': .125, 'runs': 1}
        assert after['cost_usd'] == .125 and after['by_role']['worker']['cost_usd'] == 0
        await pool.execute("UPDATE projects SET subagents='off'")
        off = (await llm_records.task_costs(['task-explore'], pool=pool))['task-explore']
        assert off['by_role']['explorer']['cost_usd'] == .125
        assert off['cost_usd'] == .125  # disabling never erases recorded spend
    run_db(test)


def test_upgrade_is_resumable_and_does_not_rewrite_existing_runs(monkeypatch):
    async def test():
        async with fresh_connection(command_timeout=60) as conn:
            await connection.ensure_schema(conn)
            await conn.execute("INSERT INTO llm_records(agent_id) VALUES ('worker-existing')")
            witness = await conn.fetch("SELECT id,ctid::text,xmin::text FROM llm_records")
            fresh_constraints = {row['conname']: row['definition'] for row in await conn.fetch(
                "SELECT conname,pg_get_constraintdef(oid) AS definition FROM pg_constraint "
                "WHERE conrelid='agent_spawns'::regclass")}
            await conn.execute("ALTER TABLE agent_spawns " + ', '.join(
                'DROP COLUMN ' + column for column in (
                    'idempotency_key', 'spawner', 'profile_id', 'delivery', 'delivered_at',
                    'cancel_reason', 'failure_class', 'failure_cause', 'attempt',
                    'child_process', 'parent_spend_known')))
            await conn.execute("ALTER TABLE projects DROP COLUMN subagents, DROP COLUMN subagent_budget_usd, "
                "DROP COLUMN subagent_task_budget_usd, DROP COLUMN subagents_per_run, DROP COLUMN subagents_per_task")
            assert await connection.schema_change_pending(conn)
            async def interrupted(_):
                pass
            with monkeypatch.context() as patch:
                patch.setattr(connection, '_finish_online', interrupted)
                await connection.ensure_schema(conn)
            assert not await conn.fetchval("SELECT convalidated FROM pg_constraint "
                "WHERE conrelid='agent_spawns'::regclass AND conname='agent_spawns_cancel_reason'")
            # A valid but non-unique predecessor must be rebuilt as unique.
            await conn.execute("CREATE INDEX idx_agent_spawns_key ON agent_spawns "
                "(parent_record_id,idempotency_key) WHERE idempotency_key IS NOT NULL")
            await connection.ensure_schema(conn)
            assert not await connection.schema_change_pending(conn)
            assert await conn.fetch("SELECT id,ctid::text,xmin::text FROM llm_records") == witness
            assert {row['conname']: row['definition'] for row in await conn.fetch(
                "SELECT conname,pg_get_constraintdef(oid) AS definition FROM pg_constraint "
                "WHERE conrelid='agent_spawns'::regclass")} == fresh_constraints
            assert await conn.fetchval("SELECT indisunique AND indisvalid FROM pg_index "
                "WHERE indexrelid=to_regclass('idx_agent_spawns_key')")
    asyncio.run(test())


def test_process_identity_compare_and_swap_never_overwrites_cancellation():
    async def test(pool):
        parent = await _task(pool)
        row = await _spawn(pool, parent)
        await agent_spawns.move(row['id'], S.ASKING, expect=S.QUEUED,
                               child_process={'run_id': 'explorer-one'}, pool=pool)
        identity = {'pid': 123, 'started': 'process-one'}
        assert await agent_spawns.record_process(row['id'], identity, pool=pool)
        assert (await agent_spawns.get(row['id'], pool=pool))['child_process'] == identity
        assert not await agent_spawns.record_process(row['id'], {'pid': 456, 'started': 'different'}, pool=pool)
        await agent_spawns.move(row['id'], S.CANCELLING, expect=S.ASKING,
                               cancel_reason='operator', pool=pool)
        assert not await agent_spawns.record_process(row['id'], identity, pool=pool)
        assert (await agent_spawns.get(row['id'], pool=pool))['child_process'] == identity
    run_db(test)
