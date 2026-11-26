"""Network recovery pauses execution, rather than spending a step retry."""
import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from tests.db_support import run_db


@pytest.mark.parametrize("review", [False, True])
@pytest.mark.parametrize('failure_typing', ['off', 'act'])
def test_network_failure_redispatches_same_attempt(monkeypatch, tmp_path, review, failure_typing):
    from looper.agents import network_pause
    from looper.agents.message import MessageEnvelope, MessageType, planner_address, worker_address
    from looper.agents.tiers import planner

    async def body(pool):
        await pool.execute("INSERT INTO projects(id,name) VALUES ('project','Network recovery')")
        await pool.execute("INSERT INTO tasks(id,project_id,requirement,status) VALUES "
                           "('task','project','Keep the work','executing')")
        await pool.execute("INSERT INTO system_settings(key,value) VALUES ('engine.network_pause.enabled','true')")
        await pool.execute("INSERT INTO system_settings(key,value) VALUES ('engine.failure_typing',$1)", failure_typing)
        agent = planner.PlannerAgent(task_id="task", runtime_root=tmp_path)
        sent = []
        agent._bus = SimpleNamespace(publish=AsyncMock(side_effect=sent.append))
        unit = planner.PlanStep('work', 0, 'Keep the instruction', key='work', review=review,
            status='completed' if review else 'dispatched', worker_id='worker',
            reviewer_id='reviewer' if review else None, retries=2,
            result={'status': 'completed', 'summary': 'Keep output'} if review else None)
        plan = agent._plan = planner.TaskPlan('plan', 'goal', 'Keep the work', str(tmp_path),
                                             [unit], status='executing', project_id='project')
        await agent._save(plan, unit, plan_row=True)
        probe = AsyncMock(return_value=True)
        monkeypatch.setattr(network_pause, 'probe', probe)
        monkeypatch.setattr(network_pause, 'PROBE_INITIAL_SECONDS', 0)
        decide = AsyncMock()
        monkeypatch.setattr(agent, '_decide', decide)
        payload = {'task_id': 'work', 'status': 'failed', 'passed': False,
            'engine_run': {'network_unavailable': {'endpoint': {'host': 'model.example.invalid', 'port': 443, 'tls': True}}}}
        envelope = MessageEnvelope.create(source=worker_address('reviewer' if review else 'worker'),
            target=planner_address('task'), message_type=MessageType.VERIFY_RESULT if review else MessageType.WORK_RESULT,
            payload=payload)
        await (agent._handle_verify_result(envelope) if review else agent._handle_work_result(envelope))
        decide.assert_not_awaited()
        assert unit.retries == 2 and plan.plan_id == 'plan' and plan.work_dir == str(tmp_path)
        dispatches = [e for e in sent if e.message_type in (MessageType.WORK_ITEM, MessageType.VERIFY_REQUEST)]
        assert len(dispatches) == 1
        assert dispatches[0].payload['instruction'] == 'Keep the instruction'
        if review:
            assert dispatches[0].payload['work_result']['summary'] == 'Keep output'
        from looper.db import network_pauses
        assert await network_pauses.get('task') is None
    run_db(body, monkeypatch, timeout=35)


def test_planner_network_exchange_restarts_without_failure_typing(monkeypatch, tmp_path):
    from looper.agents import network_pause
    from looper.agents.tiers import planner

    async def body(pool):
        await pool.execute("INSERT INTO projects(id,name) VALUES ('project','Planner recovery')")
        await pool.execute("INSERT INTO tasks(id,project_id,requirement,status) VALUES "
                           "('task','project','Plan safely','planning')")
        await pool.execute("INSERT INTO system_settings(key,value) VALUES ('engine.network_pause.enabled','true')")
        agent = planner.PlannerAgent(task_id='task', runtime_root=tmp_path)
        agent._bus = SimpleNamespace(publish=AsyncMock())
        plan = agent._plan = planner.TaskPlan('original', 'goal', 'Plan safely', str(tmp_path), project_id='project')
        await agent._save(plan, plan_row=True)
        once = AsyncMock(side_effect=[planner.PlannerTurnError('Unreachable', network_unavailable=True), 'planned'])
        monkeypatch.setattr(agent, '_exchange_with_failure_recovery', once)
        monkeypatch.setattr(agent, '_restart_network_process', AsyncMock())
        monkeypatch.setattr(network_pause, 'probe', AsyncMock(return_value=True))
        monkeypatch.setattr(network_pause, 'PROBE_INITIAL_SECONDS', 0)
        process = SimpleNamespace(launch=SimpleNamespace(profile=SimpleNamespace(provider=SimpleNamespace(base_url='https://model.example.invalid')),
                                                        settings=SimpleNamespace(environment={})))
        assert await agent._exchange('original turn', process, 0) == 'planned'
        assert once.await_count == 2 and once.await_args.args[0] == 'original turn'
        assert plan.status == 'planning' and plan.plan_id == 'original'
    run_db(body, monkeypatch, timeout=35)


def test_expiry_waits_for_operator_without_further_probes(monkeypatch):
    from looper.agents import network_pause
    from looper.db import network_pauses

    async def body(pool):
        await pool.execute("INSERT INTO projects(id,name) VALUES ('project','Bounded recovery')")
        await pool.execute("INSERT INTO tasks(id,project_id,requirement,status) VALUES "
                           "('task','project','Wait safely','executing')")
        await pool.execute("INSERT INTO system_settings(key,value) VALUES ('engine.network_pause.enabled','true')")
        now = datetime.now(timezone.utc)
        await network_pauses.put('task', {'reason': 'Model API unreachable', 'started_at': now-timedelta(hours=3),
            'deadline': now-timedelta(hours=1), 'continuation': {'endpoint': None}})
        probe = AsyncMock(return_value=True)
        monkeypatch.setattr(network_pause, 'probe', probe)
        expired = asyncio.Event()
        async def changed(row):
            if row and row['state'] == 'waiting_user':
                expired.set()
        waiting = asyncio.create_task(network_pause.wait('task', changed=changed))
        try:
            await asyncio.wait_for(expired.wait(), 3)
            assert (await network_pauses.get('task'))['state'] == 'waiting_user'
            probe.assert_not_awaited()
            assert not waiting.done()
        finally:
            waiting.cancel()
            await asyncio.gather(waiting, return_exceptions=True)
    run_db(body, monkeypatch, timeout=35)


def test_default_off_does_not_pause_or_restart(monkeypatch, tmp_path):
    from looper.agents import network_pause_settings
    from looper.agents.tiers import planner
    async def body():
        agent = planner.PlannerAgent(task_id='task', runtime_root=tmp_path)
        monkeypatch.setattr(network_pause_settings, 'load', AsyncMock(return_value=network_pause_settings.NetworkPauseSettings()))
        once = AsyncMock(side_effect=planner.PlannerTurnError('Original failure', network_unavailable=True))
        monkeypatch.setattr(agent, '_exchange_with_failure_recovery', once)
        restart = AsyncMock()
        monkeypatch.setattr(agent, '_restart_network_process', restart)
        with pytest.raises(planner.PlannerTurnError, match='Original failure'):
            await agent._exchange('unchanged input', object(), 0)
        assert once.await_count == 1
        restart.assert_not_awaited()
    asyncio.run(body())


@pytest.mark.parametrize('state', ['paused', 'waiting_user'])
def test_disabling_policy_releases_a_live_pause(monkeypatch, state):
    from looper.agents import network_pause, network_pause_settings
    from looper.db import network_pauses

    async def body(pool):
        await pool.execute("INSERT INTO projects(id,name) VALUES ('project','Network recovery')")
        await pool.execute("INSERT INTO tasks(id,project_id,requirement,status) VALUES "
                           "('task','project','Release the wait','waiting')")
        await network_pause_settings.update({'enabled': True})
        pause = await network_pauses.put('task', {'state': state, 'reason': 'Model API unavailable',
            'deadline': datetime.now(timezone.utc) + timedelta(hours=1)})
        monkeypatch.setattr(network_pause, 'PROBE_INITIAL_SECONDS', .01)
        monkeypatch.setattr(network_pause, 'PROBE_MAX_SECONDS', .02)
        monkeypatch.setattr(network_pause, 'probe', AsyncMock(return_value=False))
        started = asyncio.Event()
        changes = []
        async def changed(row):
            changes.append(row)
            started.set()
        waiting = asyncio.create_task(network_pause.wait('task', changed=changed))
        try:
            await asyncio.wait_for(started.wait(), 3)
            assert not waiting.done()
            await network_pause_settings.update({'enabled': False})
            assert await asyncio.wait_for(waiting, 3) is True
            assert changes[0]['id'] == pause['id'] and changes[-1] is None
            assert await network_pauses.get('task') is None
            ended = await pool.fetchrow('SELECT ended_at,end_reason FROM task_network_pauses WHERE id=$1', pause['id'])
            assert ended['ended_at'] is not None and ended['end_reason'] == 'disabled'
        finally:
            waiting.cancel()
            await asyncio.gather(waiting, return_exceptions=True)
    run_db(body, monkeypatch, timeout=35)


def test_restart_resumes_a_paused_planning_turn_with_the_saved_plan(monkeypatch, tmp_path):
    from looper.agents import network_pause
    from looper.agents.message import MessageEnvelope, MessageType, planner_address
    from looper.agents.tiers import planner
    from looper.db import network_pauses

    async def body(pool):
        await pool.execute("INSERT INTO projects(id,name) VALUES ('project','Restart recovery')")
        await pool.execute("INSERT INTO tasks(id,project_id,requirement,status) VALUES "
                           "('task','project','Keep this plan','waiting')")
        await pool.execute("UPDATE tasks SET work_dir=$1 WHERE id='task'", str(tmp_path))
        await pool.execute("INSERT INTO system_settings(key,value) VALUES ('engine.network_pause.enabled','true')")
        original = planner.PlannerAgent(task_id='task', runtime_root=tmp_path)
        plan = original._plan = planner.TaskPlan('saved-plan', 'goal', 'Keep this plan', str(tmp_path), project_id='project')
        await original._save(plan, plan_row=True)
        await network_pauses.put('task', {'reason': 'Model API unavailable',
            'deadline': datetime.now(timezone.utc) + timedelta(hours=1),
            'continuation': {'kind': 'planner', 'endpoint': {'host': 'model.example.invalid', 'port': 443, 'tls': True}}})
        resumed = planner.PlannerAgent(task_id='task', runtime_root=tmp_path)
        resumed._bus = SimpleNamespace(publish=AsyncMock())
        await resumed._load_plan()
        monkeypatch.setattr(network_pause, 'probe', AsyncMock(return_value=True))
        monkeypatch.setattr(network_pause, 'PROBE_INITIAL_SECONDS', 0)
        reply = object()
        ask = AsyncMock(return_value=reply)
        planned = AsyncMock()
        monkeypatch.setattr(resumed, '_ask', ask)
        monkeypatch.setattr(resumed, '_planned', planned)
        await resumed._resume(MessageEnvelope.create(source=planner_address('task'), target=planner_address('task'),
            message_type=MessageType.TASK_RESUME, payload={'task_id': 'task'}))
        assert resumed._plan.plan_id == 'saved-plan' and resumed._plan.work_dir == str(tmp_path)
        assert ask.await_args.args[0] is planner.TurnKind.PLAN
        assert planned.await_args.args[0] is resumed._plan and planned.await_args.args[1] is reply
        assert await network_pauses.get('task') is None
    run_db(body, monkeypatch, timeout=35)
