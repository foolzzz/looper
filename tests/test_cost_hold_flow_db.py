"""Spend decisions cross the real Manager, bus, Planner and task persistence boundaries."""
from unittest.mock import AsyncMock

import pytest

from looper.agents import attention, cost_guard, cost_guard_settings as settings, cost_hold_routing, cost_holds
from looper.agents.bus import MessageBus
from looper.agents.message import (MANAGER_ADDRESS, TASK_ROUTER_ADDRESS, MessageEnvelope, MessageType,
                                   channel_address, planner_address)
from looper.agents.ops.service import ManagerOps, Platform
from looper.agents.tiers.manager import ManagerAgent, ManagerConfig
from looper.agents.tiers.planner import PlannerAgent, TaskPlan, PlanStep
from looper.agents.tiers.task import TaskRouter
from looper.db import cost_holds as store, plans, tasks
from tests.db_support import DSN, run_db, v1_api

pytestmark = pytest.mark.skipif(not DSN, reason='LOOPER_TEST_DATABASE_URL is not set')


async def seed(pool, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, '_config', settings.Config())
    monkeypatch.setattr(cost_guard, '_guard', None)
    await pool.execute("INSERT INTO projects(id,name,task_budget_usd,task_budget_action) "
                       "VALUES ('p-flow','Synthetic',2,'ask')")
    await pool.execute("INSERT INTO tasks(id,project_id,requirement,work_dir,status) "
                       "VALUES ('t-flow','p-flow','Synthetic work',$1,'running')", str(tmp_path))
    await settings.update({'mode': 'enforce'}, pool=pool)
    await cost_guard.initialize(day_start=cost_guard._local_day, pool=pool)
    planner = PlannerAgent(task_id='t-flow', runtime_root=tmp_path)
    planner._plan = TaskPlan(plan_id='p', goal_id='', requirement='Synthetic work', work_dir=str(tmp_path),
                             project_id='p-flow', status='executing')
    planner._plan.steps = [PlanStep(step_id='first', index=0, instruction='Delivered', status='verified'),
                            PlanStep(step_id='next', index=1, instruction='Next work', depends_on=['first'])]
    await planner._save(planner._plan, *planner._plan.steps, plan_row=True)
    row, _ = await cost_holds.create_hold('task', 't-flow', 'equivalent_usd', 2, 2,
                                         task_id='t-flow', pool=pool)
    return planner, row


@pytest.mark.parametrize('mode', ['enforce', 'observe', 'off'])
def test_operator_dm_continues_and_planner_dispatches_next_work(monkeypatch, tmp_path, mode):
    async def body(pool):
        planner, hold = await seed(pool, tmp_path, monkeypatch)
        bus = MessageBus(runtime_root=tmp_path)
        planner._bus = bus
        router = TaskRouter(runtime_root=tmp_path)
        router._bus = bus
        platform = Platform(bus=bus, task_router=router)
        monkeypatch.setattr(cost_holds, '_platform', platform)
        await settings.update({'mode': mode}, pool=pool)
        await cost_guard.refresh()
        await planner._handle(MessageEnvelope.create(source=TASK_ROUTER_ADDRESS, target=planner.agent_address,
            message_type=MessageType.TASK_RESUME, payload={'task_id': 't-flow'}))
        assert planner._plan.step('next').status == 'pending'
        assert (await tasks.get_task('t-flow'))['status'] == 'awaiting_budget'
        manager = ManagerAgent(config=ManagerConfig(work_dir=tmp_path), runtime_root=tmp_path)
        manager._bus = bus
        manager.ops = ManagerOps(platform, url='http://127.0.0.1:9/')
        monkeypatch.setattr(cost_hold_routing.operator_identity, 'operator_sender_id',
                            AsyncMock(return_value='synthetic-operator'))
        # A fallthrough to the model is a failure, even if the text could receive a plausible reply.
        manager._run_prepared = AsyncMock(side_effect=AssertionError('hold reply reached model'))
        source = channel_address('lark', 'synthetic-bot')
        try:
            await manager._handle(MessageEnvelope.create(source=source, target=MANAGER_ADDRESS,
                message_type=MessageType.USER_MESSAGE, payload={'text': f"继续 C-{hold['id']} 4",
                    'channel_context': {'sender_id': 'synthetic-operator', 'chat_id': 'synthetic-dm',
                                        'chat_type': 'p2p'}}))
            assert (await store.get(hold['id'], pool=pool))['state'] == 'continued'
            assert (await tasks.get_task('t-flow'))['status'] == 'running'
            event = bus.get_queue(planner.agent_id).get_nowait()
            assert event.payload['type'] == 'cost_hold_continue'
            await planner._handle(event)
            step = planner._plan.step('next')
            assert step.status == 'dispatched'
            work = bus.get_queue(step.worker_id).get_nowait()
            assert work.message_type is MessageType.WORK_ITEM and work.payload['task_id'] == 'next'
            assert (await plans.load('t-flow'))['steps'][1]['status'] == 'dispatched'
            manager._run_prepared.assert_not_awaited()
        finally:
            await bus.shutdown()
    run_db(body, monkeypatch, timeout=30)


@pytest.mark.parametrize('mode', ['enforce', 'observe', 'off'])
def test_restart_keeps_wait_and_todo_lists_cost_then_expiry_stops_with_note(monkeypatch, tmp_path, mode):
    async def body(pool):
        _, hold = await seed(pool, tmp_path, monkeypatch)
        await settings.update({'mode': mode}, pool=pool)
        # Reconstruct the guard and router as startup does; no process survives this restart.
        await cost_guard.initialize(day_start=cost_guard._local_day, pool=pool)
        bus = MessageBus(runtime_root=tmp_path)
        router = TaskRouter(runtime_root=tmp_path)
        router._bus = bus
        platform = Platform(bus=bus, task_router=router)
        monkeypatch.setattr(cost_holds, '_platform', platform)
        monkeypatch.setattr(PlannerAgent, 'start', AsyncMock())
        monkeypatch.setattr(PlannerAgent, 'stop', AsyncMock())
        try:
            await router.resume_tasks(['t-flow'])
            assert router._tasks['t-flow'].status == 'awaiting_budget'
            assert (await tasks.get_task('t-flow'))['status'] == 'awaiting_budget'
            await router._handle(MessageEnvelope.create(source=planner_address('t-flow'), target=TASK_ROUTER_ADDRESS,
                message_type=MessageType.GOAL_STATUS, payload={'task_id': 't-flow', 'status': 'running'}))
            assert router._tasks['t-flow'].status == 'awaiting_budget'
            assert (await tasks.get_task('t-flow'))['status'] == 'awaiting_budget'
            manager = ManagerAgent(config=ManagerConfig(work_dir=tmp_path), runtime_root=tmp_path)
            manager._bus, manager.ops = bus, ManagerOps(platform, url='http://127.0.0.1:9/')
            monkeypatch.setattr(attention, 'enabled', AsyncMock(return_value=True))
            await manager._handle(MessageEnvelope.create(source=channel_address('web', 'synthetic-web'),
                target=MANAGER_ADDRESS, message_type=MessageType.USER_MESSAGE,
                payload={'text': '/todo', 'channel_context': {'session_id': 'synthetic'}}))
            replies = bus.get_queue('synthetic-web')
            texts = []
            while not replies.empty():
                reply = replies.get_nowait()
                if reply.message_type is MessageType.AGENT_REPLY:
                    texts.append(reply.payload['text'])
            assert any(f"C-{hold['id']}" in text and '继续' in text for text in texts)
            async with v1_api(tmp_path, pool) as call:
                status, response = await call('cost_holds.list')
                assert status == 200 and response['result']['holds'][0]['id'] == hold['id']
            await pool.execute("UPDATE cost_holds SET created_at=now()-interval '30 hours'")
            await cost_guard.refresh_day()
            assert (await store.get(hold['id'], pool=pool))['state'] == 'expired'
            await router._handle(bus.get_queue('task-router').get_nowait())
            task = await tasks.get_task('t-flow')
            assert task['status'] == 'stopped' and 'stopped at a spend hold' in task['result_summary']
            assert await store.list_pending(pool=pool) == []
        finally:
            await bus.shutdown()
    run_db(body, monkeypatch, timeout=30)


def test_api_cannot_supply_dashboard_provenance(monkeypatch, tmp_path):
    async def body(pool):
        _, hold = await seed(pool, tmp_path, monkeypatch)
        async with v1_api(tmp_path, pool) as call:
            status, _ = await call('cost_holds.decide', {'hold_id': hold['id'], 'decision': 'stop', 'via': 'dashboard'})
            assert status == 400
            assert (await store.get(hold['id'], pool=pool))['state'] == 'pending'
    run_db(body, monkeypatch, timeout=30)
