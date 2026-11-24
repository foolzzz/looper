"""Scheduled quota holds have a durable deadline and never turn routing errors into model retries."""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from tests.db_support import DSN, run_db

pytestmark = pytest.mark.skipif(not DSN, reason='requires disposable PostgreSQL')


async def seed(pool, *, scheduled=True, cron='* * * * *'):
    await pool.execute("INSERT INTO projects(id,name) VALUES('deadline-project','Deadline checks')")
    if scheduled:
        await pool.execute("INSERT INTO schedules(id,project_id,name,cron,timezone,requirement) "
                           "VALUES('deadline-schedule','deadline-project','Deadline check',$1,'UTC','Synthetic work')", cron)
    await pool.execute("INSERT INTO tasks(id,project_id,requirement,status,schedule_id) "
                       "VALUES('deadline-task','deadline-project','Synthetic work','executing',$1)",
                       'deadline-schedule' if scheduled else None)
    await pool.execute("INSERT INTO task_steps(id,task_id,step_index,step_key,instruction,status,worker_id) "
                       "VALUES('deadline-step','deadline-task',0,'work','Work','dispatched','deadline-worker')")
    await pool.execute("INSERT INTO llm_providers(id,name,vendor_type,auth) VALUES('deadline-provider','Fixture account','custom','api_key')")


async def hold(service, *, resets_at=None):
    from looper.agents.failures import Failure, FailureCause
    return await service.handle_step_result(task_id='deadline-task', step_id='deadline-step', run_id='deadline-worker',
        role='worker', provider_id='deadline-provider', failure=Failure(FailureCause.USAGE_LIMIT, resets_at=resets_at),
        target={'kind': 'worker'}, result={'status': 'failed'})


@pytest.mark.parametrize('admission_enabled', [False, True])
@pytest.mark.parametrize('cron,maximum', [('* * * * *', 60), ('0 0 * * *', 21600)])
def test_scheduled_quota_holds_expire_at_durable_deadline(monkeypatch, admission_enabled, cron, maximum):
    from looper.agents import admission, failure_handling
    from looper.agents.failures import FailureTyping
    from looper.db import step_failures

    async def body(pool):
        await seed(pool, cron=cron)
        now, sleeps, events = [datetime.now(timezone.utc)], [], []
        async def sleep(seconds):
            sleeps.append(seconds)
            now[0] += timedelta(seconds=seconds)
        async def publish(task_id, payload):
            events.append(payload)
        service = failure_handling.FailureHandlingService(pool=pool, mode=FailureTyping.ACT, clock=lambda: now[0], publish=publish)
        monkeypatch.setattr(service, '_sleep', sleep, raising=False)
        monkeypatch.setattr(service, '_schedule', lambda row, **kw: None)
        monkeypatch.setattr(admission, 'enabled', lambda: admission_enabled)
        monkeypatch.setattr(admission, '_controller', SimpleNamespace(snapshot=AsyncMock(return_value={
            'state': 'frozen', 'until': (now[0] + timedelta(days=7)).isoformat()})))
        handled = await hold(service, resets_at=now[0] + timedelta(days=7))
        deadline = await pool.fetchval('SELECT deadline FROM task_quota_waits WHERE task_id=$1', 'deadline-task')
        assert (deadline - now[0]).total_seconds() == pytest.approx(maximum, abs=.2)
        await service._timer(handled.hold)
        assert len(sleeps) == 1 and sleeps[0] == pytest.approx(maximum, abs=.2)
        released = [event for event in events if event['event'] == 'failure_hold_released']
        assert len(released) == 1 and released[0]['action'] == 'planner' and released[0]['quota_exhausted'] is True
        assert len(await step_failures.list_failures('deadline-task', pool=pool)) == 1
        assert await pool.fetchval('SELECT streak FROM step_failure_state') == 1
        assert not await step_failures.list_holds('deadline-task', pool=pool)
        assert (await step_failures.pending_releases(pool=pool))[0]['payload']['quota_exhausted'] is True
        await service.close()
    run_db(body, monkeypatch, timeout=30)


def test_restoring_quota_holds_preserves_the_first_deadline(monkeypatch):
    from looper.agents import admission, failure_handling
    from looper.agents.failures import FailureTyping

    async def body(pool):
        await seed(pool)
        now = [datetime.now(timezone.utc)]
        monkeypatch.setattr(admission, 'enabled', lambda: False)
        first = failure_handling.FailureHandlingService(pool=pool, mode=FailureTyping.ACT, clock=lambda: now[0])
        monkeypatch.setattr(first, '_schedule', lambda row, **kw: None)
        handled = await hold(first, resets_at=now[0] + timedelta(days=7))
        before = await pool.fetchval('SELECT deadline FROM task_quota_waits')
        await first.close()
        now[0] += timedelta(seconds=30)
        resumed = failure_handling.FailureHandlingService(pool=pool, mode=FailureTyping.ACT, clock=lambda: now[0])
        monkeypatch.setattr(resumed, '_schedule', lambda row, **kw: None)
        await resumed.restore_task('deadline-task')
        assert await pool.fetchval('SELECT deadline FROM task_quota_waits') == before
        waits = []
        async def sleep(seconds):
            waits.append(seconds)
            now[0] += timedelta(seconds=seconds)
        monkeypatch.setattr(resumed, '_sleep', sleep, raising=False)
        await resumed._timer(handled.hold)
        assert waits == [pytest.approx(30, abs=.2)]
        await resumed.close()
    run_db(body, monkeypatch, timeout=30)


def test_manual_tasks_have_no_scheduled_quota_deadline_and_cancel_cannot_wake(monkeypatch):
    from looper.agents import admission, failure_handling
    from looper.agents.failures import FailureTyping
    from looper.db import step_failures

    async def body(pool):
        await seed(pool, scheduled=False)
        now, events = [datetime.now(timezone.utc)], []
        async def publish(task_id, payload):
            events.append(payload)
        service = failure_handling.FailureHandlingService(pool=pool, mode=FailureTyping.ACT, clock=lambda: now[0], publish=publish)
        monkeypatch.setattr(admission, 'enabled', lambda: False)
        monkeypatch.setattr(service, '_schedule', lambda row, **kw: None)
        handled = await hold(service, resets_at=now[0] + timedelta(days=7))
        assert await pool.fetchval('SELECT count(*) FROM task_quota_waits') == 0
        await service.cancel_task('deadline-task')
        async def sleep(seconds):
            now[0] += timedelta(seconds=seconds)
        monkeypatch.setattr(service, '_sleep', sleep, raising=False)
        await service._timer(handled.hold)
        assert not [event for event in events if event['event'] == 'failure_hold_released']
        assert not await step_failures.list_holds('deadline-task', pool=pool)
        assert len(await step_failures.list_failures('deadline-task', pool=pool)) == 1
        await service.close()
    run_db(body, monkeypatch, timeout=30)


def test_account_snapshot_errors_route_the_original_failure_to_planner(monkeypatch):
    from looper.agents import admission, failure_handling
    from looper.agents.failures import FailureTyping
    from looper.db import step_failures

    async def body(pool):
        await seed(pool, scheduled=False)
        now, events = [datetime.now(timezone.utc)], []
        async def publish(task_id, payload):
            events.append(payload)
        async def sleep(seconds):
            now[0] += timedelta(seconds=seconds)
        service = failure_handling.FailureHandlingService(pool=pool, mode=FailureTyping.ACT, clock=lambda: now[0], publish=publish)
        monkeypatch.setattr(service, '_sleep', sleep, raising=False)
        monkeypatch.setattr(service, '_schedule', lambda row, **kw: None)
        monkeypatch.setattr(admission, 'enabled', lambda: True)
        snapshot = AsyncMock(side_effect=[{'state': 'frozen'}, RuntimeError('synthetic unavailable account state')])
        monkeypatch.setattr(admission, '_controller', SimpleNamespace(snapshot=snapshot))
        handled = await hold(service)
        await service._timer(handled.hold)
        released = [event for event in events if event['event'] == 'failure_hold_released']
        assert len(released) == 1 and released[0]['action'] == 'planner'
        assert 'quota_exhausted' not in released[0]
        assert len(await step_failures.list_failures('deadline-task', pool=pool)) == 1
        await service.close()
    run_db(body, monkeypatch, timeout=30)


def test_recovery_marker_survives_concurrent_ack_and_new_release(monkeypatch):
    import asyncio
    from looper.agents import admission, failure_handling
    from looper.agents.failures import Failure, FailureCause, FailureTyping
    from looper.db import step_failures

    async def body(pool):
        await seed(pool, scheduled=False)
        service = failure_handling.FailureHandlingService(pool=pool, mode=FailureTyping.ACT)
        await service.set_mode('act')
        monkeypatch.setattr(service, '_schedule', lambda row, **kw: None)
        monkeypatch.setattr(admission, 'enabled', lambda: False)
        first = await hold(service)
        first_id = first.failure['id']
        await service.release_hold('deadline-task', 'deadline-step', first_id, 'retry', operator=True)
        await pool.execute("UPDATE task_steps SET worker_id='next-worker',status='dispatched' WHERE id='deadline-step'")
        second = await service.handle_step_result(task_id='deadline-task', step_id='deadline-step', run_id='next-worker',
            role='worker', failure=Failure(FailureCause.LOGIN), target={'kind': 'worker'}, result={})
        second_id = second.failure['id']
        await asyncio.gather(service.ack_release(first_id), service.release_hold(
            'deadline-task', 'deadline-step', second_id, 'retry', operator=True))
        pending = await step_failures.pending_releases(pool=pool)
        assert [row['failure_id'] for row in pending] == [second_id]
        restored = failure_handling.FailureHandlingService(pool=pool, mode=FailureTyping.OFF)
        await restored.prepare_restore()
        assert restored.recovering
        await service.ack_release(second_id)
        assert not await step_failures.pending_releases(pool=pool)
        await service.close()
        await restored.close()
    run_db(body, monkeypatch, timeout=30)


def test_failure_stats_use_utc_days_even_with_a_different_session_zone(monkeypatch):
    from looper.db import step_failures

    async def body(pool):
        await seed(pool, scheduled=False)
        midnight = await pool.fetchval("SELECT date_trunc('day',now() AT TIME ZONE 'UTC') AT TIME ZONE 'UTC'")
        for run_id, created in (('utc-today', midnight + timedelta(hours=1)), ('utc-yesterday', midnight - timedelta(hours=1))):
            await pool.execute("INSERT INTO step_failures(task_id,step_identity,step_key,run_id,role,failure_class,"
                "failure_cause,streak,action,created_at) VALUES('deadline-task','deadline-step','work',$1,'worker',"
                "'judgment','report',1,'planner',$2)", run_id, created)
        async with pool.acquire() as conn:
            await conn.execute("SET TIME ZONE 'America/Los_Angeles'")
            figures = await step_failures.stats(pool=conn)
            assert figures['distribution'] == [{'role': 'worker', 'failure_class': 'judgment', 'streak': 1,
                                                'failures': 1, 'breakers': 0}]
    run_db(body, monkeypatch, timeout=30)


def test_environment_reminder_keeps_the_deadline_of_a_mixed_quota_target(monkeypatch):
    from looper.agents import admission, failure_handling
    from looper.agents.failures import Failure, FailureCause, FailureTyping

    async def body(pool):
        await seed(pool, cron='0 0 * * *')
        now, sleeps, events, scheduled = [datetime.now(timezone.utc)], [], [], []
        async def sleep(seconds):
            sleeps.append(seconds)
            now[0] += timedelta(seconds=seconds)
        async def publish(task_id, payload):
            events.append(payload)
        service = failure_handling.FailureHandlingService(pool=pool, mode=FailureTyping.ACT, clock=lambda: now[0], publish=publish)
        monkeypatch.setattr(service, '_sleep', sleep)
        monkeypatch.setattr(service, '_schedule', lambda row, **kw: scheduled.append(row))
        monkeypatch.setattr(admission, 'enabled', lambda: False)
        await service.handle_step_result(task_id='deadline-task', step_id='deadline-step', run_id='deadline-worker',
            role='worker', failure=Failure(FailureCause.LOGIN), target={'kind': 'worker'}, result={})
        await pool.execute("UPDATE task_steps SET status='completed',reviewer_id='quota-reviewer' WHERE id='deadline-step'")
        mixed = await service.handle_step_result(task_id='deadline-task', step_id='deadline-step', run_id='quota-reviewer',
            role='reviewer', failure=Failure(FailureCause.USAGE_LIMIT), target={'kind': 'reviewer'}, result={})
        assert mixed.hold['kind'] == 'environment' and mixed.hold['until'] is None
        await service._timer(mixed.hold)
        assert any(event['event'] == 'failure_hold_reminder' for event in events)
        assert scheduled[-1]['reminded_at'] is not None
        await service._timer(scheduled[-1])
        assert sleeps == [pytest.approx(3600, abs=.2), pytest.approx(18000, abs=.2)]
        expired = [event for event in events if event.get('quota_exhausted')]
        assert len(expired) == 1 and expired[0]['action'] == 'planner'
        await service.close()
    run_db(body, monkeypatch, timeout=30)


def test_unavailable_failure_storage_keeps_a_bounded_routing_retry(monkeypatch):
    from looper.agents import admission, failure_handling
    from looper.agents.failures import FailureTyping
    from looper.db import step_failures

    async def body(pool):
        await seed(pool, scheduled=False)
        now, waits, events, scheduled = [datetime.now(timezone.utc)], [], [], []
        async def sleep(seconds):
            waits.append(seconds)
            now[0] += timedelta(seconds=seconds)
        async def publish(task_id, payload):
            events.append(payload)
        service = failure_handling.FailureHandlingService(pool=pool, mode=FailureTyping.ACT, clock=lambda: now[0], publish=publish)
        monkeypatch.setattr(admission, 'enabled', lambda: False)
        monkeypatch.setattr(service, '_sleep', sleep)
        monkeypatch.setattr(service, '_schedule', lambda row, **kw: scheduled.append((row, kw)))
        handled = await hold(service)
        original_list, original_release = step_failures.list_holds, step_failures.release_hold
        monkeypatch.setattr(step_failures, 'list_holds', AsyncMock(side_effect=RuntimeError('synthetic storage unavailable')))
        monkeypatch.setattr(step_failures, 'release_hold', AsyncMock(side_effect=RuntimeError('synthetic storage unavailable')))
        await service._timer(handled.hold)
        assert scheduled[-1][1] == {'recovery_error': True}
        assert await original_list('deadline-task', pool=pool)
        assert not any(event['event'] == 'failure_hold_released' for event in events)
        monkeypatch.setattr(step_failures, 'list_holds', original_list)
        monkeypatch.setattr(step_failures, 'release_hold', original_release)
        await service._timer(scheduled[-1][0], recovery_error=True)
        assert waits == [pytest.approx(300, abs=.2), 60]
        released = [event for event in events if event['event'] == 'failure_hold_released']
        assert len(released) == 1 and released[0]['action'] == 'planner'
        assert len(await step_failures.list_failures('deadline-task', pool=pool)) == 1
        await service.close()
    run_db(body, monkeypatch, timeout=30)


def test_manual_breaker_with_quota_targets_never_dispatches_another_run(monkeypatch):
    from looper.agents import admission, failure_handling
    from looper.agents.failures import Failure, FailureCause, FailureTyping

    async def body(pool):
        await seed(pool, scheduled=False)
        events, sleeps = [], []
        async def publish(task_id, payload):
            events.append(payload)
        async def sleep(seconds):
            sleeps.append(seconds)
        service = failure_handling.FailureHandlingService(pool=pool, mode=FailureTyping.ACT, publish=publish)
        monkeypatch.setattr(admission, 'enabled', lambda: False)
        monkeypatch.setattr(service, '_sleep', sleep)
        monkeypatch.setattr(service, '_schedule', lambda row, **kw: None)
        for index in range(3):
            run_id = f'breaker-worker-{index}'
            await pool.execute("UPDATE task_steps SET worker_id=$1,status='dispatched' WHERE id='deadline-step'", run_id)
            handled = await service.handle_step_result(task_id='deadline-task', step_id='deadline-step', run_id=run_id,
                role='worker', failure=Failure(FailureCause.USAGE_LIMIT), target={'kind': 'worker'}, result={})
            if index < 2:
                await service.release_hold('deadline-task', 'deadline-step', handled.failure['id'], 'retry', operator=True)
        assert handled.action == 'breaker'
        events.clear()
        await service._timer(handled.hold)
        assert not events and not sleeps
        await service.close()
    run_db(body, monkeypatch, timeout=30)
