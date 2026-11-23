"""Enforcement reserves queued work and keeps disabled paths free of I/O."""
import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from looper.agents import cost_guard as cg
from looper.agents.cost_guard_settings import Config, Mode

NOW = datetime(2026, 11, 23, tzinfo=timezone.utc)


def setup_guard(monkeypatch, *, cap=2, global_cap=None):
    g = cg.Guard(Config(mode=Mode.ENFORCE, daily_usd=global_cap), clock=lambda: NOW)
    monkeypatch.setattr(cg, '_guard', g)
    monkeypatch.setattr(cg.store, 'history', AsyncMock(return_value=[]))
    monkeypatch.setattr(cg.store, 'limits', AsyncMock(return_value={}))
    monkeypatch.setattr(cg.store, 'context', AsyncMock(return_value={
        'tasks': {'task-a': {'cap': cap, 'schedule_id': None}}, 'schedules': {}}))
    return g


def launch(auth='api_key', cap=1):
    return SimpleNamespace(profile=SimpleNamespace(provider=SimpleNamespace(auth=auth)), max_budget_usd=cap)


@pytest.mark.parametrize('mode', [Mode.OFF, Mode.OBSERVE])
def test_task_gate_disabled_does_no_io(monkeypatch, mode):
    g = cg.Guard(Config(mode=mode), clock=lambda: pytest.fail('disabled gate read clock'))
    monkeypatch.setattr(cg, '_guard', g)
    monkeypatch.setattr(cg.store, 'context', AsyncMock(side_effect=AssertionError('context read')))
    result = asyncio.run(cg.task_gate('task-a', cg.RunKind.WORKER, launch(), reserve=True))
    assert result == cg.Admission()
    assert not g._pending and not g.snapshot()['recent']


def test_three_queued_workers_reserve_before_record_and_transfer_once(monkeypatch):
    async def check():
        g = setup_guard(monkeypatch)
        admissions = [await cg.task_gate('task-a', cg.RunKind.WORKER, launch(), reserve=True) for _ in range(3)]
        assert [a.decision for a in admissions] == [cg.Decision.ADMIT, cg.Decision.ADMIT, cg.Decision.ASK]
        hit = admissions[-1]
        assert hit.scope == cg.Scope(cg.BudgetScope.TASK, 'task-a', 2)
        assert hit.spent_usd == 2 and hit.currency is cg.Currency.PROVIDER
        for record in (1, 2):
            reservation = await cg.before_run(launch=launch(), run_kind='worker', task_id='task-a')
            assert reservation is admissions[record - 1].reservation
            await cg.bind(reservation, record)
        assert g._reserved[cg.BudgetScope.TASK, 'task-a', cg.Currency.PROVIDER] == 2
        for record in (1, 2):
            await cg.finish(record, cost_usd=1)
        assert g._reserved[cg.BudgetScope.TASK, 'task-a', cg.Currency.PROVIDER] == 0
        cg.store.context.return_value['tasks']['task-a']['cap'] = 4
        assert (await cg.task_gate('task-a', cg.RunKind.WORKER, launch(), reserve=True)).decision is cg.Decision.ADMIT
    asyncio.run(check())


def test_global_limit_and_currency_are_separate(monkeypatch):
    async def check():
        setup_guard(monkeypatch, cap=None, global_cap=0)
        hit = await cg.task_gate('task-a', cg.RunKind.PLANNER, launch())
        assert hit.decision is cg.Decision.ASK and hit.scope.scope is cg.BudgetScope.GLOBAL_DAY
        assert (await cg.task_gate('task-a', cg.RunKind.WORKER, launch('oauth'))).decision is cg.Decision.ADMIT
    asyncio.run(check())


def test_background_caps_remain_observation_only(monkeypatch):
    async def check():
        g = setup_guard(monkeypatch, global_cap=0)
        result = await g.admit(cg.RunKind.PROCESSOR, (cg.Scope(cg.BudgetScope.GLOBAL_DAY, 'global', 0),),
                               cg.Currency.PROVIDER, 1)
        assert result.decision is cg.Decision.ADMIT and result.would is cg.Decision.DENY
    asyncio.run(check())


def test_observe_does_not_create_empty_dispatch_queues(monkeypatch):
    async def check():
        g = setup_guard(monkeypatch)
        g.config = Config(mode=Mode.OBSERVE)
        assert await cg.before_run(launch=launch(), run_kind='worker', task_id='task-a') is not None
        assert not g._queued
    asyncio.run(check())


@pytest.mark.parametrize('mode', [Mode.ENFORCE, Mode.OBSERVE])
def test_task_end_releases_undelivered_dispatch_even_without_a_listener(monkeypatch, tmp_path, mode):
    from looper.agents.tiers.task import TaskRouter

    async def check():
        g = setup_guard(monkeypatch)
        result = await cg.task_gate('task-a', cg.RunKind.WORKER, launch(), reserve=True, step_id='one')
        result.reservation.dispatch_task_id = 'task-a'
        g.config = Config(mode=mode)
        await TaskRouter(runtime_root=tmp_path)._task_ended('task-a', 'completed')
        assert not g._pending
        assert g._reserved[cg.BudgetScope.TASK, 'task-a', cg.Currency.PROVIDER] == 0
    asyncio.run(check())


# Not marked provenance: this is the off/observe comparison itself, not a proof of where a fixture came from.
@pytest.mark.parametrize('mode', [Mode.OFF, Mode.OBSERVE])
@pytest.mark.parametrize('cap,spent', [(None,0), (2,1), (2,2)])
def test_disabled_planner_funnel_matches_fixed_baseline(monkeypatch, tmp_path, mode, cap, spent):
    import ast
    import subprocess
    from pathlib import Path
    from looper.agents.tiers import planner as module
    from looper.agents.project_execution import ProjectExecution

    revision = '6613380fa'
    source = subprocess.check_output(['git','show',f'{revision}:looper/agents/tiers/planner.py'],
                                     cwd=Path(__file__).resolve().parents[1], text=True)
    cls = next(n for n in ast.parse(source).body if isinstance(n,ast.ClassDef) and n.name=='PlannerAgent')
    method = next(n for n in cls.body if isinstance(n,ast.AsyncFunctionDef) and n.name=='_within_budget')
    namespace = dict(vars(module))
    import textwrap
    exec(compile('from __future__ import annotations\n'+textwrap.dedent(ast.get_source_segment(source,method)),
                 f'{revision}:planner.py','exec'),namespace)
    monkeypatch.setattr(cg,'_guard',cg.Guard(Config(mode=mode),clock=lambda: pytest.fail('disabled gate clock')))
    monkeypatch.setattr(module.db_llm_records,'task_costs',AsyncMock(return_value={'t':{'cost_usd':spent}}))
    p = module.PlannerAgent(task_id='t',runtime_root=tmp_path)
    p._execution = ProjectExecution(task_budget_usd=cap)
    p._stop_at_budget = AsyncMock()
    plan = module.TaskPlan(plan_id='p',goal_id='',requirement='Work',work_dir=str(tmp_path))
    plan.status='executing'
    async def check():
        expected=await namespace['_within_budget'](p,plan)
        old_calls=list(p._stop_at_budget.await_args_list)
        p._stop_at_budget.reset_mock()
        assert await p._within_budget(plan)==expected
        assert p._stop_at_budget.await_args_list==old_calls
        assert cg.current().snapshot()['counters']==[]
    asyncio.run(check())


def test_a_profile_change_cannot_reuse_a_smaller_queued_reservation(monkeypatch):
    async def check():
        g = setup_guard(monkeypatch)
        original = launch(cap=1)
        original.profile.id='first'
        assert (await cg.task_gate('task-a',cg.RunKind.WORKER,original,reserve=True,step_id='s')).decision is cg.Decision.ADMIT
        changed = launch(cap=3)
        changed.profile.id='second'
        with pytest.raises(cg.Denied):
            await cg.before_run(launch=changed,run_kind='worker',task_id='task-a',step_id='s')
        assert not g._pending
        assert g._reserved[cg.BudgetScope.TASK,'task-a',cg.Currency.PROVIDER]==0
    asyncio.run(check())


def test_accounting_failure_falls_back_without_poisoning_the_next_gate(monkeypatch):
    async def check():
        setup_guard(monkeypatch)
        cg.store.context.side_effect = OSError('synthetic read failure')
        result=await cg.task_gate('task-a',cg.RunKind.WORKER,launch())
        assert result.problem and result.reservation is None
        cg.store.context.side_effect = None
        result=await cg.task_gate('task-a',cg.RunKind.WORKER,launch())
        assert result.problem is None and result.decision is cg.Decision.ADMIT
    asyncio.run(check())


@pytest.mark.parametrize('entry', ['before_run', 'task_gate'])
def test_task_cap_refusal_precedes_a_rejected_usage_window(monkeypatch, entry):
    from datetime import timedelta
    from looper.agents import usage_windows

    async def check():
        g = setup_guard(monkeypatch, cap=1)
        g.day = g.day_start(NOW)
        g._spent[cg.BudgetScope.TASK, 'task-a', cg.Currency.PROVIDER] = 1
        held = usage_windows.Hold(NOW + timedelta(hours=1), usage_windows.HoldReason.REJECTED, True, 'five_hour')
        monkeypatch.setattr(usage_windows, 'hold_for', AsyncMock(return_value=held))
        notified = AsyncMock()
        monkeypatch.setattr(usage_windows, 'notify_hold', notified)
        configured = launch()
        configured.profile.provider.id = 'provider-synthetic'
        if entry == 'before_run':
            with pytest.raises(cg.Denied) as refused:
                await cg.before_run(launch=configured, run_kind='worker', task_id='task-a')
            admission = refused.value.admission
        else:
            admission = await cg.task_gate('task-a', cg.RunKind.WORKER, configured, reserve=True)
        assert admission.decision is cg.Decision.ASK
        assert admission.scope == cg.Scope(cg.BudgetScope.TASK, 'task-a', 1)
        assert admission.spent_usd == 1 and admission.currency is cg.Currency.PROVIDER
        assert not g._pending and all(value == 0 for value in g._reserved.values())
        notified.assert_not_awaited()
    asyncio.run(check())


def test_a_queued_reservation_is_released_when_a_usage_window_becomes_rejected(monkeypatch):
    from datetime import timedelta
    from looper.agents import usage_windows

    async def check():
        g = setup_guard(monkeypatch)
        configured = launch()
        configured.profile.provider.id = 'provider-synthetic'
        queued = await cg.task_gate('task-a', cg.RunKind.WORKER, configured, reserve=True, step_id='step-a')
        monkeypatch.setattr(usage_windows, 'hold_for', AsyncMock(return_value=usage_windows.Hold(
            NOW + timedelta(hours=1), usage_windows.HoldReason.REJECTED, True, 'five_hour')))
        monkeypatch.setattr(usage_windows, 'notify_hold', AsyncMock())
        with pytest.raises(cg.Held):
            await cg.before_run(launch=configured, run_kind='worker', task_id='task-a', step_id='step-a')
        assert queued.reservation.token not in g._pending
        assert all(value == 0 for value in g._reserved.values())
    asyncio.run(check())
