"""Observation persistence, restart and main's two accounting currencies on disposable PostgreSQL."""
import pytest

from looper.agents import cost_guard as cg
from looper.agents.cost_guard_settings import Config, Mode
from looper.db import llm_records
from tests.db_support import DSN, run_db

pytestmark = pytest.mark.skipif(not DSN, reason='LOOPER_TEST_DATABASE_URL is not set')


@pytest.mark.parametrize('run_kind', ['worker', 'explorer'])
def test_observe_three_concurrent_runs_survive_restart_and_settle_to_recorded_costs(monkeypatch, run_kind):
    async def body(pool):
        # Real rows, including the task's effective cap, drive admission; no mocked accounting totals.
        await pool.execute("INSERT INTO projects (id, name, task_budget_usd) VALUES ('project-cost', 'Cost sample', 2)")
        await pool.execute("INSERT INTO tasks (id, project_id, requirement, status) "
                           "VALUES ('task-cost', 'project-cost', 'Synthetic task', 'running')")
        g = cg.Guard(Config(mode=Mode.OBSERVE), pool=pool)
        await g.rebuild()
        admissions = []
        for _ in range(3):
            scopes = g.scopes(cg.RunKind(run_kind), 'task-cost', None, cg.Currency.PROVIDER)
            admission = await g.admit(cg.RunKind(run_kind), scopes, cg.Currency.PROVIDER, 1)
            row_id = await llm_records.start_record(
                agent_id=run_kind + '-cost', run_kind=run_kind, task_id='task-cost',
                metadata=admission.reservation.metadata(), pool=pool)
            g.bind(admission.reservation, row_id)
            admissions.append((admission, row_id))
        assert [ad.would for ad, _ in admissions] == [cg.Decision.ADMIT, cg.Decision.ADMIT, cg.Decision.ASK]
        assert await pool.fetchval("SELECT count(*) FROM llm_records WHERE metadata ? 'reservation'") == 3
        projected = await llm_records.get_record(admissions[2][1], pool=pool)
        assert 'reservation' not in projected['metadata'] and 'cost_guard' not in projected['metadata']
        restarted = cg.Guard(Config(mode=Mode.OBSERVE), pool=pool)
        await restarted.rebuild()
        task = next(c for c in restarted.snapshot()['counters'] if c['scope'] == 'task')
        assert task['reserved_usd'] == 3
        for _, row_id in admissions:
            await llm_records.finish_record(row_id, status='completed', cost_usd=.25, cost_source='provider_price', pool=pool)
            restarted.finish(row_id, .25)
        totals = (await llm_records.task_costs(['task-cost'], pool=pool))['task-cost']
        task = next(c for c in restarted.snapshot()['counters'] if c['scope'] == 'task')
        assert task['reserved_usd'] == 0 and task['spent_usd'] == float(totals['cost_usd']) == .75
        # A subscription sample remains separate both before the run and after rebuilding actual costs.
        row_id = await llm_records.start_record(
            agent_id=run_kind + '-cost', run_kind=run_kind, task_id='task-cost',
            metadata={'profile': {'provider': {'auth': 'oauth'}}}, pool=pool)
        await llm_records.finish_record(row_id, status='completed', cost_usd=.1, pool=pool)
        await restarted.rebuild()
        counters = [c for c in restarted.snapshot()['counters'] if c['scope'] == 'task']
        assert {c['currency']: c['spent_usd'] for c in counters} == {'provider_priced_usd': .75, 'equivalent_usd': .1}
        assert restarted.estimate(cg.RunKind(run_kind), cg.Currency.EQUIVALENT, None)[1] is cg.EstimateSource.FLOOR
    run_db(body, monkeypatch, timeout=30)


def test_existing_run_and_interrupted_reservations_do_not_become_p95_samples(monkeypatch):
    async def body(pool):
        record = await llm_records.start_record(agent_id='processor:sample', metadata={'cost_guard_schedule_id': 'missing'}, pool=pool)
        await llm_records.finish_record(record, status='completed', cost_usd=.2, pool=pool)
        g = cg.Guard(Config(mode=Mode.OBSERVE, daily_usd=.1), pool=pool)
        await g.rebuild()
        assert g.estimate(cg.RunKind.PROCESSOR, cg.Currency.PROVIDER, None) == (.2, cg.EstimateSource.P95)
        global_row = next(c for c in g.snapshot()['counters'] if c['scope'] == 'global_day')
        assert global_row['spent_usd'] == .2 and global_row['limit_usd'] == .1
    run_db(body, monkeypatch, timeout=30)


@pytest.mark.parametrize('scope,entry', [
    (scope, entry) for scope in ('role_day', 'global_day', 'schedule_day')
    for entry in ('before_run', 'admit', 'schedule')
] + [('global_day', 'task_gate')])
@pytest.mark.parametrize('settled', [False, True])
def test_two_guards_count_other_process_reservations_and_spend(monkeypatch, scope, entry, settled):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from looper.agents.cost_guard_settings import RoleLimits

    async def body(pool):
        await pool.execute("INSERT INTO projects(id,name) VALUES ('shared-project','Shared')")
        await pool.execute("INSERT INTO schedules(id,project_id,name,cron,timezone,requirement,daily_budget_usd,"
                           "daily_budget_equivalent_usd) VALUES ('shared-schedule','shared-project','Shared','0 * * * *','UTC','test',"
                           "$1,5)", 1 if scope == 'schedule_day' else None)
        config = Config(mode=Mode.ENFORCE, daily_usd=1 if scope == 'global_day' else None,
                        roles={'processor': RoleLimits(daily_usd=1)} if scope == 'role_day' else {})
        guards = [cg.Guard(config, pool=pool), cg.Guard(config, pool=pool)]
        for guard in guards:
            await guard.rebuild()  # Both processes already established today's counters.
        monkeypatch.setattr(cg.cost_alerts, 'flush', AsyncMock())
        launch = SimpleNamespace(profile=SimpleNamespace(provider=SimpleNamespace(auth='api_key')),
                                 max_budget_usd=.8)
        monkeypatch.setattr(cg, '_guard', guards[0])
        reservation = await cg.before_run(launch=launch, run_kind='processor', schedule_id='shared-schedule')
        record = await llm_records.start_record(agent_id='processor:shared', run_kind='processor',
                                                metadata=reservation.metadata(), pool=pool)
        await cg.bind(reservation, record)
        if settled:
            await llm_records.finish_record(record, status='completed', cost_usd=.8,
                                            cost_source='provider_price', pool=pool)
            await cg.finish(record, cost_usd=.8)
        # This process has not restarted or crossed a day boundary.
        monkeypatch.setattr(cg, '_guard', guards[1])
        if entry == 'before_run':
            with pytest.raises(cg.Denied) as denied:
                await cg.before_run(launch=launch, run_kind='processor', schedule_id='shared-schedule')
            admission = denied.value.admission
        elif entry == 'admit':
            chain = guards[1].scopes(cg.RunKind.PROCESSOR, None, 'shared-schedule', cg.Currency.PROVIDER)
            admission = await guards[1].admit(cg.RunKind.PROCESSOR, chain, cg.Currency.PROVIDER, .8)
        elif entry == 'schedule':
            monkeypatch.setattr(cg, '_schedule_launch', AsyncMock(return_value=(launch, cg.RunKind.PROCESSOR)))
            admission = await cg.observe_schedule({'id': 'shared-schedule', 'kind': 'subscription',
                'run_budget_usd': .8, 'daily_budget_usd': 1 if scope == 'schedule_day' else None,
                'daily_budget_equivalent_usd': 5}, defer_alerts=True)
        else:
            await pool.execute("INSERT INTO tasks(id,project_id,requirement) "
                               "VALUES ('shared-task','shared-project','test')")
            admission = await cg.task_gate('shared-task', cg.RunKind.WORKER, launch, reserve=True)
        assert admission.decision in (cg.Decision.DENY, cg.Decision.ASK)
        assert admission.scope.scope.value == scope
        assert admission.spent_usd == pytest.approx(.8)
        assert not guards[1]._pending
        # The lower-level API must also refresh, with local pending tokens counted once.
        scopes = guards[0].scopes(cg.RunKind.PROCESSOR, None, 'shared-schedule', cg.Currency.PROVIDER)
        result = await guards[0].admit(cg.RunKind.PROCESSOR, scopes, cg.Currency.PROVIDER, .3)
        assert result.decision is cg.Decision.DENY and result.spent_usd == pytest.approx(.8)
    run_db(body, monkeypatch, timeout=30)


def test_unknown_model_cost_survives_guard_restart_and_api_contract(monkeypatch):
    from jsonschema import Draft202012Validator
    from looper.agents.v1_domains.cost_guard import _COUNTER

    async def body(pool):
        record = await llm_records.start_record(agent_id='processor:unpriced', pool=pool)
        await llm_records.finish_record(record, status='completed', model='haiku',
                                        metadata={'model_reported': 'claude-haiku-5-5'}, pool=pool)
        g = cg.Guard(Config(mode=Mode.OBSERVE, daily_usd=1), pool=pool)
        await g.rebuild()
        snapshot = g.snapshot()
        row = next(c for c in snapshot['counters'] if c['scope'] == 'global_day')
        assert row['cost_status'] == 'unknown' and row['unknown_models'] == ['claude-haiku-5-5']
        assert 'claude-haiku-5-5' in snapshot['problem']
        Draft202012Validator(_COUNTER).validate(row)
    run_db(body, monkeypatch, timeout=30)
