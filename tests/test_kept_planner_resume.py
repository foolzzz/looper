"""A resumed Planner adopts held runs before considering a new dispatch."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from looper.agents.agent_settings import AgentType
from looper.agents.kept_run import AdoptFailure, AdoptOutcome, AdoptResult, RunHold
from looper.agents.message import MessageEnvelope, MessageType, planner_address, worker_address
from looper.agents.tiers.planner import PlanStep, PlannerAgent, ReviewStage, StepReviewRole, TaskPlan


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


@pytest.mark.parametrize('stage', [ReviewStage.BUILTIN, ReviewStage.ROLE])
@pytest.mark.parametrize('review_first', [False, True])
def test_parallel_adopted_results_enforce_the_task_cap_after_the_last_run(
    tmp_path, monkeypatch, agent_db, stage, review_first,
):
    from looper.agents.project_execution import ProjectExecution
    from looper.agents.tiers.planner_protocol import Decision, DecisionReply
    from looper.db import llm_records

    async def run():
        planner = PlannerAgent(task_id='task-resume', runtime_root=tmp_path,
                               adopter=AsyncMock(return_value=AdoptResult(AdoptOutcome.ADOPTED)))
        planner._bus = SimpleNamespace(publish=AsyncMock(), request=AsyncMock(
            return_value=SimpleNamespace(payload={'killed': False})))
        planner._execution = ProjectExecution(task_budget_usd=1)
        planner._ask = AsyncMock(return_value=DecisionReply(Decision.ACCEPT, '', '', (), 'accepted'))
        reviewing = PlanStep(
            step_id='step-review', index=0, instruction='review the change', status='completed',
            worker_id='worker-review', reviewer_id='reviewer-original', review=True,
            result={'status': 'completed'}, review_stage=stage,
            review_position=0 if stage is ReviewStage.ROLE else None,
            review_roles=[StepReviewRole('security'), StepReviewRole('style')])
        working = PlanStep(step_id='step-work', index=1, instruction='finish independent work',
                           status='dispatched', worker_id='worker-original')
        plan = planner._plan = TaskPlan('plan-resume', 'goal-resume', 'check the change', str(tmp_path),
                                        steps=[reviewing, working], status='executing')
        monkeypatch.setattr(llm_records, 'task_costs', AsyncMock(return_value={'task-resume': {'cost_usd': 1}}))
        await planner._handle(MessageEnvelope.create(
            source=planner_address('old'), target=planner.agent_address,
            message_type=MessageType.TASK_RESUME, payload={}))
        assert planner._adopted_runs == {'reviewer-original', 'worker-original'}

        review_result = MessageEnvelope.create(
            source=worker_address('reviewer-original'), target=planner.agent_address,
            message_type=MessageType.VERIFY_RESULT,
            payload={'task_id': reviewing.step_id, 'passed': True, 'summary': 'checks passed'})
        work_result = MessageEnvelope.create(
            source=worker_address('worker-original'), target=planner.agent_address,
            message_type=MessageType.WORK_RESULT,
            payload={'task_id': working.step_id, 'status': 'completed', 'summary': 'work completed'})
        first, last = (review_result, work_result) if review_first else (work_result, review_result)
        await planner._handle(first)
        assert plan.status == 'executing'  # The other adopted run must be allowed to finish.
        await planner._handle(last)

        assert working.status == 'verified'
        verdict = reviewing.result['verify_passed'] if stage is ReviewStage.BUILTIN else \
            reviewing.result['role_reviews'][0]['passed']
        assert verdict is True
        assert plan.status == 'failed'
        assert agent_db.store.plans['task-resume']['status'] == 'failed'
        assert agent_db.store.steps['task-resume'][working.step_id]['status'] == 'verified'
        messages = [call.args[0] for call in planner._bus.publish.await_args_list]
        assert not any(message.message_type is MessageType.VERIFY_REQUEST for message in messages)
        assert messages[-1].payload['fail_kind'] == 'task_budget'

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


@pytest.mark.parametrize('stage', [ReviewStage.BUILTIN, ReviewStage.ROLE])
def test_an_adopted_review_reaching_the_task_cap_stops_before_the_next_review(
    tmp_path, monkeypatch, agent_db, stage,
):
    from looper.agents.project_execution import ProjectExecution
    from looper.db import llm_records

    async def run():
        planner = PlannerAgent(task_id='task-resume', runtime_root=tmp_path,
                               adopter=AsyncMock(return_value=AdoptResult(AdoptOutcome.ADOPTED)))
        planner._bus = SimpleNamespace(publish=AsyncMock(), request=AsyncMock(
            return_value=SimpleNamespace(payload={'killed': False})))
        planner._execution = ProjectExecution(task_budget_usd=1)
        step = PlanStep(step_id='step-resume', index=0, instruction='check the change', status='completed',
                        worker_id='worker-original', reviewer_id='reviewer-original', review=True,
                        result={'status': 'completed'}, review_stage=stage,
                        review_position=0 if stage is ReviewStage.ROLE else None,
                        review_roles=[StepReviewRole('security'), StepReviewRole('style')])
        plan = planner._plan = TaskPlan('plan-resume', 'goal-resume', 'check the change', str(tmp_path),
                                        steps=[step], status='executing')
        monkeypatch.setattr(llm_records, 'task_costs', AsyncMock(return_value={'task-resume': {'cost_usd': 1}}))
        await planner._handle(MessageEnvelope.create(
            source=planner_address('old'), target=planner.agent_address,
            message_type=MessageType.TASK_RESUME, payload={}))
        assert step.reviewer_id in planner._adopted_runs

        # A stale result must not retire the Reviewer still running across the restart.
        for source in ('reviewer-stale', 'reviewer-original'):
            await planner._handle(MessageEnvelope.create(
                source=worker_address(source), target=planner.agent_address,
                message_type=MessageType.VERIFY_RESULT,
                payload={'task_id': step.step_id, 'passed': True, 'summary': 'checks passed'}))
            if source == 'reviewer-stale':
                assert plan.status == 'executing'
                assert step.reviewer_id in planner._adopted_runs

        assert plan.status == 'failed'
        assert step.reviewer_id not in planner._adopted_runs
        verdict = step.result['verify_passed'] if stage is ReviewStage.BUILTIN else step.result['role_reviews'][0]['passed']
        assert verdict is True
        messages = [call.args[0] for call in planner._bus.publish.await_args_list]
        assert not any(message.message_type is MessageType.VERIFY_REQUEST for message in messages)
        assert messages[-1].payload['fail_kind'] == 'task_budget'
        assert agent_db.store.plans['task-resume']['status'] == 'failed'

    asyncio.run(run())
