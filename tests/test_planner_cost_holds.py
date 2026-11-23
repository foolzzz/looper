"""A budget hold preserves in-flight work and resumes the saved plan without new prompt text."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from looper.agents import cost_guard as cg, cost_guard_settings as settings, cost_holds
from looper.agents.project_execution import ProjectExecution, TaskBudgetAction
from looper.agents.tiers import planner_budget
from looper.agents.tiers.planner import PlannerAgent, TaskPlan, PlanStep


@pytest.mark.parametrize('status', ['stopping', 'completed', 'failed'])
@pytest.mark.parametrize('cap', [None, 0])
def test_closing_turn_uses_actual_admission(monkeypatch, tmp_path, status, cap):
    from looper.agents.tiers.planner import PlannerTurnError
    from tests.runtime_model_helpers import builtin_launch

    async def check():
        monkeypatch.setattr(cg, '_guard', cg.Guard(settings.Config(mode=settings.Mode.ENFORCE)))
        monkeypatch.setattr(cg.store, 'history', AsyncMock(return_value=[]))
        monkeypatch.setattr(cg.store, 'limits', AsyncMock(return_value={}))
        monkeypatch.setattr(cg.store, 'context', AsyncMock(return_value={
            'tasks': {'task-a': {'cap': cap, 'schedule_id': None}}, 'schedules': {}}))
        monkeypatch.setattr(planner_budget, '_launch', AsyncMock(return_value=builtin_launch()))
        monkeypatch.setattr(planner_budget.db_tasks, 'get_task', AsyncMock(return_value={'status': status}))
        p = PlannerAgent(task_id='task-a', runtime_root=tmp_path)
        p._plan = TaskPlan(plan_id='p', goal_id='', requirement='Work', work_dir=str(tmp_path), status=status)
        p._run_turn = AsyncMock(return_value='Closing summary')
        if cap is None:
            assert await p._turn('Close the task') == 'Closing summary'
            p._run_turn.assert_awaited_once()
        else:
            with pytest.raises(PlannerTurnError, match='admission refused'):
                await p._turn('Close the task')
            p._run_turn.assert_not_awaited()
        assert cg.current().snapshot()['recent'][-1]['decision'] == ('admit' if cap is None else 'ask')
    asyncio.run(check())


@pytest.mark.parametrize('action', ['ask', 'fail', 'wrap_up'])
def test_budget_action_keeps_running_work_or_uses_existing_stop(monkeypatch, tmp_path, action):
    async def check():
        g = cg.Guard(settings.Config(mode=settings.Mode.ENFORCE))
        monkeypatch.setattr(cg, '_guard', g)
        p = PlannerAgent(task_id='task-a', runtime_root=tmp_path)
        p._execution = ProjectExecution(task_budget_usd=2, task_budget_action=TaskBudgetAction(action))
        plan = TaskPlan(plan_id='plan-a', goal_id='', requirement='Synthetic work', work_dir=str(tmp_path))
        plan.status = 'executing'
        plan.steps = [PlanStep(step_id='one', index=0, instruction='One', status='dispatched', worker_id='worker-one')]
        p._plan = plan
        p._activity = AsyncMock()
        p._save = AsyncMock()
        p._stop_at_budget = AsyncMock()
        monkeypatch.setattr(planner_budget, '_launch', AsyncMock(return_value=SimpleNamespace()))
        monkeypatch.setattr(planner_budget.db_tasks, 'get_task', AsyncMock(return_value={'status': 'running'}))
        admission = cg.Admission(cg.Decision.ASK, cg.Decision.ASK, scope=cg.Scope(cg.BudgetScope.TASK,'task-a',2),
                                 currency=cg.Currency.PROVIDER, spent_usd=2)
        monkeypatch.setattr(cg, 'task_gate', AsyncMock(return_value=admission))
        create = AsyncMock(return_value=({'id': 1}, True))
        monkeypatch.setattr(cost_holds, 'create_hold', create)
        assert await p._within_budget(plan) is False
        assert plan.steps[0].status == 'dispatched'
        if action == 'ask':
            create.assert_awaited_once()
            p._stop_at_budget.assert_not_awaited()
            p._activity.assert_awaited_once()
        else:
            p._stop_at_budget.assert_awaited_once_with(plan, 2, 2)
            create.assert_not_awaited()
    asyncio.run(check())


def test_wake_redecides_finished_results_without_restarting_running_workers(monkeypatch, tmp_path):
    async def check():
        p = PlannerAgent(task_id='task-a', runtime_root=tmp_path)
        p._plan = TaskPlan(plan_id='p', goal_id='', requirement='Work', work_dir=str(tmp_path))
        p._plan.status = 'executing'
        finished = PlanStep(step_id='done', index=0,instruction='Done',status='deciding')
        running = PlanStep(step_id='live', index=1,instruction='Live',status='dispatched',worker_id='worker-live')
        p._plan.steps = [finished, running]
        p._decide_again = AsyncMock()
        p._dispatch_ready = AsyncMock()
        p._read_execution = AsyncMock()
        await planner_budget.resume(p)
        p._decide_again.assert_awaited_once_with(p._plan, finished)
        p._dispatch_ready.assert_awaited_once_with(p._plan)
        assert running.worker_id == 'worker-live' and running.status == 'dispatched'
    asyncio.run(check())


@pytest.mark.parametrize('review', [False, True])
def test_late_admission_refusal_is_control_state_without_a_model_report(monkeypatch, tmp_path, review):
    from looper.agents.bus import MessageBus
    from looper.agents.message import MessageType, planner_address
    from looper.agents.tiers.worker import WorkerPool, WorkerConfig, WorkerProcess, RunReply
    from looper.agents.agent_settings import AgentType

    async def check():
        monkeypatch.setattr(cg, '_guard', cg.Guard(settings.Config(mode=settings.Mode.ENFORCE)))
        bus=MessageBus(runtime_root=tmp_path)
        pool=WorkerPool(config=WorkerConfig(),runtime_root=tmp_path)
        pool._bus=bus
        wp=WorkerProcess(worker_id='worker-held',task_id='s',plan_id='p',parent_task_id='t',
                         role=AgentType.REVIEWER if review else AgentType.WORKER)
        admission=cg.Admission(cg.Decision.ASK,cg.Decision.ASK,scope=cg.Scope(cg.BudgetScope.TASK,'t',2),
                               currency=cg.Currency.PROVIDER,spent_usd=2,estimate_usd=1)
        run=AsyncMock(side_effect=cg.Denied('must never enter model input',admission=admission))
        await pool._finish_run(wp,None,RunReply(planner_address('t'),'',{}),run)
        envelope=bus.get_queue('planner-t').get_nowait()
        assert envelope.message_type is MessageType.SYSTEM_EVENT
        assert 'must never enter model input' not in str(envelope.payload)
        p=PlannerAgent(task_id='t',runtime_root=tmp_path)
        p._plan=TaskPlan(plan_id='p',goal_id='',requirement='Work',work_dir=str(tmp_path))
        p._plan.status='executing'
        step=PlanStep(step_id='s',index=0,instruction='Work',status='completed' if review else 'dispatched',
                      worker_id='finished-worker' if review else 'worker-held',
                      reviewer_id='worker-held' if review else None,
                      result={'status':'completed','summary':'Delivered'} if review else None)
        p._plan.steps=[step]
        p._save=AsyncMock()
        held=AsyncMock()
        monkeypatch.setattr(planner_budget,'hold',held)
        await p._handle(envelope)
        held.assert_awaited_once()
        assert step.status==('completed' if review else 'pending')
        assert step.reviewer_id is None
        assert step.result==({'status':'completed','summary':'Delivered'} if review else None)
    asyncio.run(check())
