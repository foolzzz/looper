"""A resumed Planner adopts held runs before considering a new dispatch."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from looper.agents.agent_settings import AgentType
from looper.agents.kept_run import AdoptFailure, AdoptOutcome, AdoptResult, RunHold
from looper.agents.message import MessageEnvelope, MessageType, planner_address
from looper.agents.tiers.planner import PlannerAgent


@pytest.mark.parametrize('review', [False, True])
@pytest.mark.parametrize('outcome', list(AdoptOutcome) + [None])
def test_resume_adopts_or_repeats_the_same_attempt(tmp_path, review, outcome):
    async def run():
        failure = AdoptFailure.LOG_SHORT if outcome is AdoptOutcome.FAILED else None
        adopter = AsyncMock(return_value=AdoptResult(outcome, failure)) if outcome is not None else None
        planner = PlannerAgent(task_id='task-resume', runtime_root=tmp_path, adopter=adopter)
        planner._bus = SimpleNamespace()
        step = SimpleNamespace(step_id='step-resume', status='completed' if review else 'dispatched',
                               worker_id='worker-original', reviewer_id='reviewer-original')
        planner._plan = SimpleNamespace(status='executing', steps=[step], has_failed=False, is_complete=False,
                                        progress_summary=lambda: 'running')
        planner._activity = AsyncMock()
        planner._dispatch_step = AsyncMock()
        planner._spawn_reviewer = AsyncMock()
        planner._dispatch_ready = AsyncMock()
        planner._report_progress = AsyncMock()
        envelope = MessageEnvelope.create(source=planner_address('old'), target=planner.agent_address,
                                          message_type=MessageType.TASK_RESUME, payload={})
        await planner._resume(envelope)
        run_id = 'reviewer-original' if review else 'worker-original'
        if adopter is not None:
            adopter.assert_awaited_once_with(run_id, RunHold('task-resume', 'step-resume',
                                                   AgentType.REVIEWER if review else AgentType.WORKER))
        dispatch = planner._spawn_reviewer if review else planner._dispatch_step
        if outcome is AdoptOutcome.ADOPTED:
            dispatch.assert_not_awaited()
            planner._activity.assert_any_await('phase', f'Worker {run_id} carried on across the restart', worker=run_id)
        else:
            dispatch.assert_awaited_once_with(planner._plan, step, same_attempt=True)
            if outcome is AdoptOutcome.FAILED:
                planner._activity.assert_any_await('phase', f'Worker {run_id} could not carry on across the restart '
                                                  '(log_short); running the step again', worker=run_id)

    asyncio.run(run())


@pytest.mark.parametrize('enabled', [False, True])
def test_adoption_precedes_the_budget_gate_for_new_dispatches(tmp_path, enabled):
    async def run():
        planner = PlannerAgent(task_id='task-resume', runtime_root=tmp_path,
                               adopter=AsyncMock() if enabled else None)
        planner._plan = SimpleNamespace(status='executing')
        for name in ('_refresh_project', '_read_changes', '_read_gates', '_resume', '_deliver_changes',
                     '_deliver_gate_messages', '_deliver_room_messages', '_deliver_waits', '_pending_changes'):
            setattr(planner, name, AsyncMock())
        planner._within_budget = AsyncMock(return_value=False)
        envelope = MessageEnvelope.create(source=planner_address('old'), target=planner.agent_address,
                                          message_type=MessageType.TASK_RESUME, payload={})
        await planner._handle(envelope)
        planner._resume.assert_awaited_once_with(envelope)
        assert planner._within_budget.await_count == (0 if enabled else 1)

    asyncio.run(run())


@pytest.mark.parametrize('review', [False, True])
def test_the_task_cap_blocks_new_work_without_ending_an_adopted_run(tmp_path, monkeypatch, review):
    from looper.agents.project_execution import ProjectExecution
    from looper.db import llm_records

    async def run():
        planner = PlannerAgent(task_id='task-resume', runtime_root=tmp_path)
        planner._execution = ProjectExecution(task_budget_usd=1)
        running = SimpleNamespace(status='completed' if review else 'dispatched',
                                  worker_id='worker-original', reviewer_id='reviewer-original')
        plan = SimpleNamespace(status='executing', steps=[running])
        planner._adopted_runs.add('reviewer-original' if review else 'worker-original')
        planner._stop_at_budget = AsyncMock()
        monkeypatch.setattr(llm_records, 'task_costs', AsyncMock(return_value={'task-resume': {'cost_usd': 2}}))
        assert not await planner._within_budget(plan)
        planner._stop_at_budget.assert_not_awaited()
        # Once the carried-on result has been accepted, ordinary task-cap stopping applies again.
        running.status = 'verified'
        assert not await planner._within_budget(plan)
        planner._stop_at_budget.assert_awaited_once_with(plan, 2, 1)

    asyncio.run(run())
