"""Window waits preserve the plan and resume admission without model-visible notices."""
import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest

from looper.agents import cost_guard as cg, cost_guard_settings as settings, cost_holds
from looper.agents.bus import MessageBus
from looper.agents.message import MessageEnvelope, MessageType, planner_address
from looper.agents.tiers import planner_budget
from looper.agents.tiers.planner import PlannerAgent, PlanStep, TaskPlan

NOW = datetime(2026, 11, 23, tzinfo=timezone.utc)


def planner(monkeypatch, tmp_path):
    guard = cg.Guard(settings.Config(mode=settings.Mode.ENFORCE), clock=lambda: NOW)
    monkeypatch.setattr(cg, '_guard', guard)
    p = PlannerAgent(task_id='task-window', runtime_root=tmp_path)
    p._bus = MessageBus(runtime_root=tmp_path)
    p._plan = TaskPlan(plan_id='p', goal_id='', requirement='Work', work_dir=str(tmp_path), status='executing')
    p._activity = AsyncMock()
    p._save = AsyncMock()
    p._run_turn = AsyncMock(return_value='model answer')
    monkeypatch.setattr(planner_budget.db_tasks, 'get_task', AsyncMock(return_value={'status': 'running'}))
    monkeypatch.setattr(planner_budget, '_launch', AsyncMock(return_value=object()))
    return p, guard


def test_window_wait_is_distinct_from_spend_ask_and_rechecks_on_idle(monkeypatch, tmp_path):
    async def check():
        p, guard = planner(monkeypatch, tmp_path)
        p._plan.steps = [PlanStep(step_id='live', index=0, instruction='Live',
                                 status='dispatched', worker_id='worker-live')]
        until = NOW + timedelta(minutes=5)
        gate = AsyncMock(return_value=cg.Admission(cg.Decision.HOLD, until=until, reason='rejected'))
        monkeypatch.setattr(cg, 'task_gate', gate)
        create_hold = AsyncMock(side_effect=AssertionError('window wait became a spend question'))
        monkeypatch.setattr(cost_holds, 'create_hold', create_hold)
        assert await p._within_budget(p._plan) is False
        assert p._plan.status == 'executing' and p._plan.steps[0].worker_id == 'worker-live'
        assert '等待用量窗口重置' in p._activity.await_args.args[1]
        p._run_turn.assert_not_awaited()
        # The ordinary idle loop wakes a window wait once it expires; no new timer can survive stop.
        guard.clock = lambda: until
        await p._idle_tick()
        event = p._bus.get_queue(p.agent_id).get_nowait()
        assert event.payload == {'type': 'usage_window_resume'}
        create_hold.assert_not_awaited()
        assert gate.await_count == 1
    asyncio.run(check())


@pytest.mark.parametrize('review', [False, True])
def test_worker_window_refusal_has_no_failure_report_and_resumes_the_saved_step(monkeypatch, tmp_path, review):
    from looper.agents.agent_settings import AgentType
    from looper.agents.tiers.worker import RunReply, WorkerConfig, WorkerPool, WorkerProcess

    async def check():
        p, _guard = planner(monkeypatch, tmp_path)
        pool = WorkerPool(config=WorkerConfig(), runtime_root=tmp_path)
        pool._bus = p._bus
        worker = WorkerProcess(worker_id='worker-window', task_id='step-window', plan_id='p',
                               parent_task_id=p._task_id, role=AgentType.REVIEWER if review else AgentType.WORKER)
        step = PlanStep(step_id=worker.task_id, index=0, instruction='Work',
                        status='completed' if review else 'dispatched',
                        worker_id='finished-worker' if review else worker.worker_id,
                        reviewer_id=worker.worker_id if review else None,
                        result={'status': 'completed', 'summary': 'Delivered'} if review else None)
        p._plan.steps = [step]
        until = NOW + timedelta(minutes=5)
        await pool._finish_run(worker, None, RunReply(planner_address(p._task_id), '', {}),
                               AsyncMock(side_effect=cg.Held(until, 'rejected')))
        event = p._bus.get_queue(p.agent_id).get_nowait()
        assert event.message_type is MessageType.SYSTEM_EVENT
        assert event.payload['type'] == 'usage_dispatch_held'
        assert 'error' not in event.payload and 'summary' not in event.payload
        assert worker.status == 'stopped' and worker.result is None
        await p._handle(event)
        assert step.status == ('completed' if review else 'pending')
        assert step.reviewer_id is None and step.retries == 0
        assert step.result == ({'status': 'completed', 'summary': 'Delivered'} if review else None)
        assert '等待用量窗口重置' in p._activity.await_args.args[1]
        p._read_execution = AsyncMock()
        p._spawn_reviewer = AsyncMock()
        p._dispatch_ready = AsyncMock()
        await planner_budget.resume(p)
        if review:
            p._spawn_reviewer.assert_awaited_once_with(p._plan, step)
        p._dispatch_ready.assert_awaited_once_with(p._plan)
    asyncio.run(check())


def test_a_late_planner_window_refusal_stays_control_state(monkeypatch, tmp_path):
    async def check():
        p, _guard = planner(monkeypatch, tmp_path)
        p._handle_event = AsyncMock(side_effect=cg.Held(NOW + timedelta(minutes=5), 'quota_failure'))
        await p._handle(MessageEnvelope.create(source=p.agent_address, target=p.agent_address,
                                              message_type=MessageType.SYSTEM_EVENT, payload={'type': 'idle'}))
        assert p._plan.status == 'executing'
        assert '等待用量窗口重置' in p._activity.await_args.args[1]
        p._run_turn.assert_not_awaited()
    asyncio.run(check())


def test_restart_rechecks_an_unstarted_planning_turn_instead_of_failing_it(monkeypatch, tmp_path):
    async def check():
        p, _guard = planner(monkeypatch, tmp_path)
        p._plan.status = 'planning'
        p._read_execution = AsyncMock()
        p._plan_and_dispatch = AsyncMock()
        p._handle_event = AsyncMock(side_effect=AssertionError('planning fell through to failed restart'))
        await p._handle(MessageEnvelope.create(source=p.agent_address, target=p.agent_address,
                                              message_type=MessageType.TASK_RESUME, payload={}))
        p._plan_and_dispatch.assert_awaited_once()
        assert p._plan_and_dispatch.await_args.args[0].payload['requirement'] == 'Work'
    asyncio.run(check())


def test_a_window_refused_before_start_never_records_a_failed_attempt(monkeypatch, tmp_path):
    from looper.agents import attempts
    from looper.agents.tiers.worker import RunContext, WorkerConfig, WorkerPool
    from looper.db import memory_lesson_injection
    from tests.runtime_model_helpers import builtin_launch

    async def check():
        pool = WorkerPool(config=WorkerConfig(), runtime_root=tmp_path)
        launch = builtin_launch()
        context = RunContext(worker_id='worker-window', parent_task_id='task-window', step_id=None,
                             work_dir=str(tmp_path), prompt='', is_verify=False, launch=launch,
                             runtime=launch.runtime, seat=None, run_timeout=0, started_at=NOW,
                             hosted=False, command=[], attempt_id=42)
        pool._prepare = AsyncMock(return_value=context)
        pool._execute_once = AsyncMock(side_effect=cg.Held(NOW + timedelta(minutes=5), 'rejected'))
        finished = AsyncMock()
        monkeypatch.setattr(attempts, 'worker_finished', finished)
        delivered = AsyncMock()
        monkeypatch.setattr(memory_lesson_injection, 'mark_delivered', delivered)
        with pytest.raises(cg.Held):
            await pool._execute(MessageEnvelope.create(source=planner_address('task-window'),
                                                      target=planner_address('task-window'),
                                                      message_type=MessageType.WORK_ITEM, payload={}))
        pool._prepare.assert_awaited_once()
        finished.assert_not_awaited()
        delivered.assert_not_awaited()
    asyncio.run(check())


def test_window_wake_checks_caps_before_dispatch_without_a_phantom_planner_turn(monkeypatch, tmp_path):
    from looper.agents.project_execution import ProjectExecution, TaskBudgetAction

    async def check():
        p, _guard = planner(monkeypatch, tmp_path)
        p._execution = ProjectExecution(task_budget_action=TaskBudgetAction.ASK)
        events = []
        p._plan.steps = [PlanStep(step_id='ready', index=0, instruction='Work')]

        async def gate(*args, **kwargs):
            events.append('gate')
            if p._plan.steps[0].status == 'dispatched':
                return cg.Admission(cg.Decision.ASK, scope=cg.Scope(cg.BudgetScope.TASK, p._task_id, 1),
                                    currency=cg.Currency.PROVIDER, spent_usd=1)
            return cg.Admission()

        async def dispatch(plan):
            events.append('dispatch')
            plan.steps[0].status = 'dispatched'

        monkeypatch.setattr(cg, 'task_gate', gate)
        create = AsyncMock(return_value=({'id': 1}, True))
        monkeypatch.setattr(cost_holds, 'create_hold', create)
        p._dispatch_ready = dispatch
        for name in ('_read_execution', '_refresh_project', '_pending_changes', '_read_changes', '_read_gates',
                     '_deliver_changes', '_deliver_gate_messages', '_deliver_room_messages', '_deliver_waits'):
            setattr(p, name, AsyncMock())
        p._blocked_conditions = AsyncMock(return_value=False)
        await p._handle(MessageEnvelope.create(source=p.agent_address, target=p.agent_address,
                                              message_type=MessageType.SYSTEM_EVENT,
                                              payload={'type': 'usage_window_resume'}))
        assert events == ['gate', 'dispatch']
        create.assert_not_awaited()
        p._run_turn.assert_not_awaited()
    asyncio.run(check())
