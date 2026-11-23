"""A budget hold preserves in-flight work and resumes the saved plan without new prompt text."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from looper.agents import cost_guard as cg, cost_guard_settings as settings, cost_holds
from looper.agents.project_execution import ProjectExecution, TaskBudgetAction
from looper.agents.tiers import planner_budget
from looper.agents.tiers.planner import PlannerAgent, TaskPlan, PlanStep


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
