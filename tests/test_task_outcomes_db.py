"""Immutable per-run terminal observations, time ranges and split billing."""

from datetime import datetime, timedelta, timezone
import json

import pytest

from looper.agents import attempts
from looper.agents.attempts import Decision, StopCause, TaskOutcome, Trigger
from looper.db import advisor_advice, agent_spawns, llm_records, task_outcomes, tasks
from tests.db_support import DSN, run_db

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")


async def seed(pool):
    await pool.execute("INSERT INTO projects(id,name) VALUES ('p','Synthetic project')")
    await pool.execute("INSERT INTO tasks(id,requirement,project_id) VALUES ('t','Synthetic task','p')")
    await pool.execute("INSERT INTO task_steps(id,task_id,step_index,instruction,step_key) VALUES "
                       "('s','t',0,'Synthetic step','s1'), ('g','t',1,'Synthetic gate','g1'), "
                       "('w','t',2,'Synthetic wait','w1')")


@pytest.mark.parametrize('outcome,cause', [
    (TaskOutcome.COMPLETED, StopCause.PLANNER_END), (TaskOutcome.STOPPED, StopCause.USER_STOP),
    (TaskOutcome.CANCELLED, StopCause.USER_CANCEL), (TaskOutcome.CANCELLED, StopCause.MANAGER_CANCEL),
    (TaskOutcome.FAILED, StopCause.TASK_BUDGET), (TaskOutcome.STOPPED, StopCause.RESTART),
])
def test_one_immutable_outcome_per_run(monkeypatch, outcome, cause):
    async def test(pool):
        await attempts.record_outcome('t', outcome, cause)
        await attempts.record_outcome('t', TaskOutcome.COMPLETED, StopCause.TEARDOWN)
        assert await pool.fetchval('SELECT count(*) FROM task_outcomes') == 1
        assert await pool.fetchval('SELECT stop_cause FROM task_outcomes') == cause
        await pool.execute("UPDATE tasks SET run_no=run_no+1 WHERE id='t'")
        await attempts.record_outcome('t', TaskOutcome.COMPLETED, StopCause.PLANNER_END)
        assert await pool.fetchval('SELECT count(*) FROM task_outcomes') == 2

    run_db(test, monkeypatch, setup=seed)


def test_orphan_batch_records_restart_and_reset_preserves_attempt_and_outcome_history(monkeypatch):
    async def test(pool):
        await pool.execute("INSERT INTO tasks(id,requirement,project_id,status) VALUES "
                           "('keep','Synthetic kept task','p','running'), ('other','Synthetic orphan','p','running')")
        await pool.execute("UPDATE tasks SET status='running' WHERE id='t'")
        attempt = await attempts.dispatch_attempt('t', 's', 's1')
        assert await tasks.mark_orphaned_tasks_stopped(keep=['keep']) == 2
        assert await pool.fetchval("SELECT count(*) FROM task_outcomes WHERE stop_cause='restart'") == 2
        assert await pool.fetchval("SELECT status FROM tasks WHERE id='keep'") == 'running'
        assert await tasks.reset_task_for_restart('t')
        row = await pool.fetchrow('SELECT * FROM step_attempts WHERE id=$1', attempt)
        assert row['step_id'] is None and row['step_key'] == 's1' and row['failure_kind'] == 'stopped'
        assert await pool.fetchval("SELECT run_no FROM tasks WHERE id='t'") == 2
        await attempts.record_outcome('t', TaskOutcome.COMPLETED, StopCause.PLANNER_END)
        assert await pool.fetchval("SELECT count(*) FROM task_outcomes WHERE task_id='t'") == 2

    run_db(test, monkeypatch, setup=seed)


def test_orphan_status_and_restart_continue_when_observation_writers_fail(monkeypatch, caplog):
    from looper.db import step_attempts, task_outcomes

    async def broken(*args, **kwargs):
        raise RuntimeError('synthetic observation failure')

    async def test(pool):
        await pool.execute("UPDATE tasks SET status='running' WHERE id='t'")
        monkeypatch.setattr(step_attempts, 'close_attempts_batch', broken)
        monkeypatch.setattr(task_outcomes, 'record_outcomes', broken)
        assert await tasks.mark_orphaned_tasks_stopped() == 1
        assert await pool.fetchval("SELECT status FROM tasks WHERE id='t'") == 'stopped'
        assert await tasks.reset_task_for_restart('t')
        assert await pool.fetchval("SELECT run_no FROM tasks WHERE id='t'") == 2
        assert 'synthetic observation failure' in caplog.text

    run_db(test, monkeypatch, setup=seed)


def test_attempt_planning_and_unattributed_costs_partition_task_costs_on_each_basis(monkeypatch):
    async def test(pool):
        attempt = await attempts.dispatch_attempt('t', 's', 's1')
        planning = await llm_records.start_record(agent_id='planner-t', task_id='t')
        advise = await llm_records.start_record(agent_id='planner-t', task_id='t')
        deciding = await llm_records.start_record(agent_id='planner-t', task_id='t')
        worker = await llm_records.start_record(agent_id='worker-s', task_id='t')
        reviewer = await llm_records.start_record(agent_id='reviewer-s', task_id='t')
        await attempts.worker_started(attempt, worker, 'synthetic-profile')
        await attempts.link_record(attempt, reviewer)
        advisor = await llm_records.start_record(agent_id='consult-test', run_kind='advisor', task_id='t',
                                                parent_record_id=advise)
        judge = await llm_records.start_record(agent_id='consult-judge', run_kind='advice_check', task_id='t',
                                              parent_record_id=advisor)
        consult = await agent_spawns.create(kind='advisor', task_id='t', step_id='s',
                                             parent_record_id=advise, brief={}, inputs={}, budget_usd=1)
        await advisor_advice.record_question(spawn_id=consult['id'], task_id='t', step_id='s', attempt=1,
                                            source='optional', kind='plan_review', question={})
        await attempts.link_advice(attempt, consult['id'])
        assert await pool.fetchval('SELECT attempt_id FROM llm_records WHERE id=$1', advise) == attempt
        assert await pool.fetchval('SELECT attempt_id FROM advisor_advice WHERE spawn_id=$1', consult['id']) == attempt
        await attempts.decide_attempt(attempt, Decision.ACCEPT, record_id=deciding)
        # A consult on a later attempt must preserve its parent's earlier attribution.
        later = await attempts.open_attempt('t', 's', 's1', 1, Trigger.RETRY)
        await attempts.link_advice(later, consult['id'])
        assert await pool.fetchval('SELECT attempt_id FROM llm_records WHERE id=$1', advise) == attempt
        unattributed = await llm_records.start_record(agent_id='worker-unattributed', task_id='t')
        for index, record in enumerate((planning, advise, deciding, worker, reviewer, advisor, judge, unattributed)):
            await llm_records.finish_record(record, status='completed', cost_usd=(index+1)/10)
            if index % 2:
                await llm_records.add_metadata(record, {'accounting_basis': 'subscription_equivalent'})
        # Attribute descendants to the nearest ancestor with an attempt; preserve every remaining run.
        rows = await pool.fetch(f"""
            WITH RECURSIVE attributed AS (
                SELECT id,attempt_id FROM llm_records WHERE task_id='t' AND attempt_id IS NOT NULL
                UNION ALL
                SELECT r.id,a.attempt_id FROM llm_records r JOIN attributed a ON r.parent_record_id=a.id
                WHERE r.attempt_id IS NULL AND r.task_id='t'
            )
            SELECT CASE WHEN a.attempt_id IS NOT NULL THEN 'attempt'
                        WHEN r.run_kind='planner' THEN 'planning' ELSE 'unattributed' END AS bucket,
                   sum(cost_usd) FILTER (WHERE NOT {llm_records.EQUIVALENT}) AS provider,
                   sum(cost_usd) FILTER (WHERE {llm_records.EQUIVALENT}) AS equivalent
            FROM llm_records r LEFT JOIN attributed a USING(id) WHERE r.task_id='t'
            GROUP BY 1
        """)
        assert {row['bucket'] for row in rows} == {'attempt', 'planning', 'unattributed'}
        total = await llm_records.spend_since(None, project_id='p')
        assert sum(row['provider'] or 0 for row in rows) == pytest.approx(total['provider_priced_usd'])
        assert sum(row['equivalent'] or 0 for row in rows) == pytest.approx(total['equivalent_usd'])
        costs = (await llm_records.task_costs(['t']))['t']['cost_usd']
        assert total['provider_priced_usd'] + total['equivalent_usd'] == pytest.approx(costs)

    run_db(test, monkeypatch, setup=seed)


def test_snapshot_counts_steps_wait_union_and_task_costs_on_both_bases(monkeypatch):
    async def test(pool):
        start = datetime.now(timezone.utc) - timedelta(minutes=10)
        await pool.execute("UPDATE tasks SET created_at=$1 WHERE id='t'", start-timedelta(seconds=1))
        planner = await llm_records.start_record(agent_id='planner-t', task_id='t')
        await pool.execute('UPDATE llm_records SET created_at=$2 WHERE id=$1', planner, start)
        await llm_records.finish_record(planner, status='completed', cost_usd=.1)
        attempt = await attempts.open_attempt('t', 's', 's1', 1, Trigger.FIRST)
        for agent, cost, metadata in [('worker-one', .2, {}), ('worker-two', .3, {
                'accounting_basis': 'subscription_equivalent'}), ('manager', 99, {}), ('worker-unpriced', None, {})]:
            record = await llm_records.start_record(agent_id=agent, task_id='t', metadata=metadata)
            await llm_records.finish_record(record, status='completed', cost_usd=cost)
        late = await llm_records.start_record(agent_id='reviewer-late', task_id='t')
        await attempts.decide_attempt(attempt, Decision.ACCEPT)
        await pool.execute("INSERT INTO task_user_gates(step_id,task_id,topic,opened_at,closed_at) "
                           "VALUES ('g','t','Synthetic gate',$1,$2)", start + timedelta(seconds=10),
                           start + timedelta(seconds=40))
        await pool.execute("INSERT INTO task_waits(step_id,task_id,topic,spec,opened_at,closed_at) "
                           "VALUES ('w','t','Synthetic wait','{}',$1,$2)", start + timedelta(seconds=20),
                           start + timedelta(seconds=50))
        await attempts.record_outcome('t', TaskOutcome.COMPLETED, StopCause.PLANNER_END)
        row = await pool.fetchrow('SELECT * FROM task_outcomes')
        assert row is not None
        assert row['started_at'] == start
        assert row['human_wait_ms'] == 40000
        assert (row['steps_accepted'], row['steps_failed'], row['attempts'], row['first_pass_steps']) == (1, 0, 1, 1)
        assert row['provider_priced_usd'] == pytest.approx(.3)
        assert row['equivalent_usd'] == pytest.approx(.3)
        assert row['unpriced_runs'] == 1
        costs = (await llm_records.task_costs(['t']))['t']['cost_usd']
        assert row['provider_priced_usd'] + row['equivalent_usd'] == pytest.approx(costs)
        await llm_records.finish_record(late, status='completed', cost_usd=.9)
        await attempts.record_outcome('t', TaskOutcome.COMPLETED, StopCause.TEARDOWN)
        assert await pool.fetchval('SELECT provider_priced_usd FROM task_outcomes') == row['provider_priced_usd']
        await pool.execute("UPDATE tasks SET run_no=2 WHERE id='t'")
        new_planner = await llm_records.start_record(agent_id='planner-t', task_id='t')
        await llm_records.finish_record(new_planner, status='completed', cost_usd=.7)
        await attempts.record_outcome('t', TaskOutcome.COMPLETED, StopCause.PLANNER_END)
        row = await pool.fetchrow('SELECT * FROM task_outcomes WHERE run_no=2')
        assert row['provider_priced_usd'] == pytest.approx(.7)
        assert row['equivalent_usd'] == 0 and row['attempts'] == 0 and row['human_wait_ms'] == 0

    run_db(test, monkeypatch, setup=seed)


def test_snapshot_cost_query_filters_records_by_requested_tasks(monkeypatch):
    async def test(pool):
        class CheckedDatabase:
            async def execute(self, sql, *args):
                # Force index selection on this small fixture; inspect the real snapshot query.
                async with pool.acquire() as conn, conn.transaction():
                    await conn.execute('SET LOCAL enable_seqscan=off')
                    plan = json.loads(await conn.fetchval('EXPLAIN (FORMAT JSON) ' + sql, *args))[0]['Plan']
                    nodes = [plan]
                    indexed_tasks = False
                    while nodes:
                        node = nodes.pop()
                        indexed_tasks |= (node.get('Index Name') == 'idx_llm_records_task'
                                          and 'task_id = ANY' in node.get('Index Cond', ''))
                        nodes.extend(node.get('Plans', []))
                    assert indexed_tasks, plan
                    return await conn.execute(sql, *args)

        await task_outcomes.record_outcomes(['t'], 'completed', 'planner_end', pool=CheckedDatabase())
        assert await pool.fetchval("SELECT count(*) FROM task_outcomes WHERE task_id='t'") == 1

    run_db(test, monkeypatch, setup=seed)
