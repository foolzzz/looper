"""Atomic failure accounting, versioned holds, and durable execution identities."""
from datetime import datetime, timedelta, timezone

import pytest

from tests.db_support import DSN, run_db

pytestmark = pytest.mark.skipif(not DSN, reason="requires disposable PostgreSQL")


async def seed(pool):
    await pool.execute("INSERT INTO projects(id,name) VALUES ('p-failure','Failure checks')")
    await pool.execute("INSERT INTO tasks(id,project_id,requirement,status) VALUES "
                       "('t-failure','p-failure','Synthetic task','executing'),"
                       "('t-other','p-failure','Another synthetic task','executing')")
    await pool.execute("INSERT INTO task_steps(id,task_id,step_index,step_key,instruction,status,worker_id) "
                       "VALUES ('s-failure','t-failure',0,'work','Do work','dispatched','worker-1')")


def test_breaker_boundary_and_reset_follow_maintainer_contract(monkeypatch):
    from looper.agents.failure_handling import FailureHandlingService
    from looper.agents.failures import Failure, FailureCause, FailureTyping
    from looper.db import step_failures

    async def body(pool):
        await seed(pool)
        service = FailureHandlingService(pool=pool, mode=FailureTyping.ACT, jitter=lambda: 1)
        for index, cause in enumerate((FailureCause.CONNECTION_FAILED, FailureCause.REPORT, FailureCause.LOGIN), 1):
            run_id = f"worker-{index}"
            await pool.execute("UPDATE task_steps SET status='dispatched',worker_id=$1 WHERE id='s-failure'", run_id)
            result = await service.handle_step_result(task_id='t-failure', step_id='s-failure', run_id=run_id,
                attempt=index, role='worker', failure=Failure(cause), target={'kind': 'worker', 'source': run_id},
                result={'status': 'failed'})
            assert result.action == ('hold' if index == 1 else 'planner' if index == 2 else 'breaker')
            assert result.failure['streak'] == index
        assert (await step_failures.list_holds('t-failure', pool=pool))[0]['kind'] == 'breaker'
        # A negative review is neither a failed run nor the success that clears its preceding failures.
        await service.note_success(task_id='t-failure', step_id='s-failure', run_id='review-no', role='reviewer',
                                   outcome='not_passed', target={'kind': 'reviewer'})
        assert await pool.fetchval("SELECT streak FROM step_failure_state") == 3
        await pool.execute("UPDATE task_steps SET status='dispatched',worker_id='worker-success' WHERE id='s-failure'")
        await service.note_success(task_id='t-failure', step_id='s-failure', run_id='worker-success', role='worker',
                                   target={'kind': 'worker'})
        assert await pool.fetchval("SELECT streak FROM step_failure_state") == 0
        assert len(await step_failures.list_failures('t-failure', pool=pool)) == 3
        await service.close()
    run_db(body, monkeypatch, timeout=30)


def test_failure_result_and_success_are_idempotent_across_restart(monkeypatch):
    from looper.agents.failure_handling import FailureHandlingService
    from looper.agents.failures import Failure, FailureCause, FailureTyping
    from looper.db import step_failures

    async def body(pool):
        await seed(pool)
        args = dict(task_id='t-failure', step_id='s-failure', run_id='worker-1', attempt=0, role='worker',
                    failure=Failure(FailureCause.LOGIN), target={'kind': 'worker'}, result={'status': 'failed'})
        first = FailureHandlingService(pool=pool, mode=FailureTyping.ACT)
        handled = await first.handle_step_result(**args)
        duplicate = await first.handle_step_result(**args)
        assert duplicate.action == 'duplicate'
        assert duplicate.failure['id'] == handled.failure['id']
        await first.close()
        second = FailureHandlingService(pool=pool, mode=FailureTyping.ACT)
        await second.restore_task('t-failure')
        assert await pool.fetchval("SELECT streak FROM step_failure_state") == 1
        assert len(await step_failures.list_failures('t-failure', pool=pool)) == 1
        await second.close()
    run_db(body, monkeypatch, timeout=30)


def test_hold_release_is_versioned_and_operator_only(monkeypatch):
    import asyncio
    from looper.agents.failure_handling import FailureHandlingService, FailureHandlingError
    from looper.agents.failures import Failure, FailureCause, FailureTyping
    from looper.db import step_failures

    async def body(pool):
        await seed(pool)
        events = []
        async def publish(task_id, payload):
            events.append((task_id, payload))
        service = FailureHandlingService(pool=pool, publish=publish, mode=FailureTyping.ACT)
        handled = await service.handle_step_result(task_id='t-failure', step_id='s-failure', run_id='worker-1',
            role='worker', attempt=0, failure=Failure(FailureCause.LOGIN), target={'kind': 'worker'}, result={})
        failure_id = handled.failure['id']
        with pytest.raises(FailureHandlingError, match='operator'):
            await service.release_hold('t-failure', 's-failure', failure_id, 'retry')
        with pytest.raises(FailureHandlingError):
            await service.release_hold('t-other', 's-failure', failure_id, 'retry', operator=True)
        releases = await asyncio.gather(*(service.release_hold('t-failure', 's-failure', failure_id, 'retry',
                                                             operator=True) for _ in range(2)), return_exceptions=True)
        assert sum(isinstance(item, dict) for item in releases) == 1
        assert len([item for _, item in events if item['event'] == 'failure_hold_released']) == 1
        assert not await step_failures.list_holds('t-failure', pool=pool)
        assert await pool.fetchval("SELECT streak FROM step_failure_state") == 1
        await service.close()
    run_db(body, monkeypatch, timeout=30)


def test_failure_history_survives_step_and_record_cleanup(monkeypatch):
    from looper.agents.failure_handling import FailureHandlingService
    from looper.agents.failures import Failure, FailureCause, FailureTyping
    from looper.db import step_failures

    async def body(pool):
        await seed(pool)
        record_id = await pool.fetchval("INSERT INTO llm_records(agent_id,status) VALUES ('worker-1','failed') RETURNING id")
        service = FailureHandlingService(pool=pool, mode=FailureTyping.ACT)
        await service.handle_step_result(task_id='t-failure', step_id='s-failure', run_id='worker-1', role='worker',
            failure=Failure(FailureCause.REPORT), record_id=record_id, target={'kind': 'worker'}, result={})
        await pool.execute("DELETE FROM llm_records WHERE id=$1", record_id)
        await pool.execute("DELETE FROM task_steps WHERE id='s-failure'")
        rows = await step_failures.list_failures('t-failure', pool=pool)
        assert len(rows) == 1 and rows[0]['step_id'] is None and rows[0]['record_id'] is None
        assert rows[0]['step_identity'] == 's-failure' and rows[0]['failure_cause'] == 'report'
        # Recreating the same step does not turn an automatic replan into a reset.
        await pool.execute("INSERT INTO task_steps(id,task_id,step_index,step_key,instruction,status,worker_id) "
                           "VALUES ('s-new','t-failure',0,'work','Do work','dispatched','worker-2')")
        handled = await service.handle_step_result(task_id='t-failure', step_id='s-new', run_id='worker-2',
            role='worker', failure=Failure(FailureCause.REPORT), target={'kind': 'worker'}, result={})
        assert handled.failure['streak'] == 2
        await service.close()
    run_db(body, monkeypatch, timeout=30)


def test_transient_and_quota_waits_are_finite(monkeypatch):
    from looper.agents.failure_handling import FailureHandlingService
    from looper.agents.failures import Failure, FailureCause, FailureTyping

    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    service = FailureHandlingService(mode=FailureTyping.ACT, clock=lambda: now, jitter=lambda: .8)
    assert service.hold_until(Failure(FailureCause.CONNECTION_FAILED), 1) == now + timedelta(seconds=96)
    assert service.hold_until(Failure(FailureCause.CONNECTION_FAILED), 2) == now + timedelta(seconds=480)
    assert service.hold_until(Failure(FailureCause.USAGE_LIMIT), 8) == now + timedelta(minutes=60)
    for reset in (None, now - timedelta(seconds=1), now + timedelta(days=9)):
        assert service.hold_until(Failure(FailureCause.USAGE_LIMIT, resets_at=reset), 1) == now + timedelta(minutes=5)
    reset = now + timedelta(days=8)
    assert service.hold_until(Failure(FailureCause.USAGE_LIMIT, resets_at=reset), 1) == reset + timedelta(seconds=60)


def test_failure_history_is_complete_across_pages_and_record_cleanup(monkeypatch):
    from looper.agents.failure_handling import FailureHandlingService
    from looper.agents.failures import Failure, FailureCause, FailureTyping
    from looper.db import step_failures

    async def body(pool):
        await seed(pool)
        service = FailureHandlingService(pool=pool, mode=FailureTyping.ACT)
        for index in range(205):
            run_id = f'run-{index}'
            await pool.execute("UPDATE task_steps SET worker_id=$1,status='dispatched' WHERE id='s-failure'", run_id)
            await service.handle_step_result(task_id='t-failure', step_id='s-failure', run_id=run_id, role='worker',
                failure=Failure(FailureCause.REPORT), target={'kind': 'worker'}, result={})
        ids, after = [], 0
        while True:
            page = await step_failures.list_failures('t-failure', after_id=after, limit=50, pool=pool)
            if not page:
                break
            ids.extend(row['id'] for row in page)
            after = page[-1]['id']
        assert len(ids) == len(set(ids)) == 205 and ids == sorted(ids)
        await service.operator_reset('t-failure', operator=True, actor='operator-test')
        assert await pool.fetchval('SELECT streak FROM step_failure_state') == 0
        audit = await pool.fetchval("SELECT summary FROM task_journal WHERE summary LIKE 'Operator restart%' ORDER BY id DESC LIMIT 1")
        assert 'operator-test' in audit and '205' in audit and 'work' in audit
        assert len(await step_failures.list_failures('t-failure', limit=201, pool=pool)) == 201
        await service.close()
    run_db(body, monkeypatch, timeout=40)


def test_mode_exit_cancels_waits_and_drains_each_hold_once(monkeypatch):
    import asyncio
    from looper.agents.failure_handling import FailureHandlingService
    from looper.agents.failures import Failure, FailureCause, FailureTyping
    from looper.db import step_failures

    async def body(pool):
        await seed(pool)
        events = []
        async def publish(task_id, payload):
            events.append(payload)
        service = FailureHandlingService(pool=pool, publish=publish, mode=FailureTyping.ACT)
        await service.handle_step_result(task_id='t-failure', step_id='s-failure', run_id='worker-1', role='worker',
            failure=Failure(FailureCause.CONNECTION_FAILED), target={'kind': 'worker'}, result={'status': 'failed'})
        waiter = asyncio.create_task(service.wait_turn(10000))
        await asyncio.sleep(0)
        await service.set_mode(None)
        assert await waiter is False
        assert not await step_failures.list_holds('t-failure', pool=pool)
        assert len(await step_failures.list_failures('t-failure', pool=pool)) == 1
        releases = [event for event in events if event['event'] == 'failure_hold_released']
        assert len(releases) == 1 and releases[0]['action'] == 'planner'
        assert releases[0]['targets'][0]['result'] == {'status': 'failed'}
        assert not service._timers
        await service.close()
    run_db(body, monkeypatch, timeout=30)


def test_concurrent_review_failures_share_a_counter_and_preserve_each_target(monkeypatch):
    import asyncio
    from looper.agents.failure_handling import FailureHandlingService
    from looper.agents.failures import Failure, FailureCause, FailureTyping
    from looper.db import step_failures

    async def body(pool):
        await seed(pool)
        for seat in ('one', 'two'):
            await pool.execute("INSERT INTO task_steps(id,task_id,step_index,step_key,instruction,status,worker_id,kind) "
                "VALUES($1,'t-failure',1,$2,'Review work','dispatched',$3,'review')", f'seat-{seat}', seat, f'review-{seat}')
        service = FailureHandlingService(pool=pool, mode=FailureTyping.ACT)
        results = await asyncio.gather(*(service.handle_step_result(task_id='t-failure', step_id='s-failure',
            run_id=f'review-{seat}', role='reviewer', failure=Failure(FailureCause.LOGIN),
            target={'kind': 'panel', 'unit_step_id': f'seat-{seat}', 'source': f'review-{seat}'}, result={})
            for seat in ('one', 'two')))
        assert {result.failure['streak'] for result in results} == {1, 2}
        holds = await step_failures.list_holds('t-failure', pool=pool)
        assert len(holds) == 1
        assert {item['target']['unit_step_id'] for item in holds[0]['targets']} == {'seat-one', 'seat-two'}
        assert await pool.fetchval("SELECT count(*) FROM task_steps WHERE id LIKE 'seat-%' AND status='pending'") == 2
        await service.close()
    run_db(body, monkeypatch, timeout=30)


def test_cancelling_one_task_does_not_end_another_tasks_turn_wait(monkeypatch):
    import asyncio
    from looper.agents.failure_handling import FailureHandlingService
    from looper.agents.failures import FailureTyping

    async def body(pool):
        await seed(pool)
        service = FailureHandlingService(pool=pool, mode=FailureTyping.ACT)
        cancelled = asyncio.create_task(service.wait_turn(60, task_id='t-failure'))
        sibling = asyncio.create_task(service.wait_turn(.05, task_id='t-other'))
        await asyncio.sleep(0)
        await service.cancel_task('t-failure')
        assert await cancelled is False
        assert await sibling is True
        await service.close()
    run_db(body, monkeypatch, timeout=30)


def test_environment_reminder_and_breaker_warning_survive_restart(monkeypatch):
    import asyncio
    from looper.agents import failure_handling, self_check
    from looper.agents.failures import Failure, FailureCause, FailureTyping

    async def body(pool):
        await seed(pool)
        events = []
        async def publish(task_id, payload):
            events.append(payload)
        service = failure_handling.FailureHandlingService(pool=pool, publish=publish, mode=FailureTyping.ACT)
        monkeypatch.setattr(failure_handling, '_service', service)
        handled = await service.handle_step_result(task_id='t-failure', step_id='s-failure', run_id='worker-1',
            role='worker', failure=Failure(FailureCause.LOGIN), target={'kind': 'worker'}, result={})
        hold = handled.hold
        await service.close()
        await pool.execute("UPDATE step_holds SET created_at=now()-interval '2 hours'")
        resumed = failure_handling.FailureHandlingService(pool=pool, publish=publish, mode=FailureTyping.ACT)
        monkeypatch.setattr(failure_handling, '_service', resumed)
        await resumed.restore_task('t-failure')
        for _ in range(50):
            if any(event['event'] == 'failure_hold_reminder' for event in events):
                break
            await asyncio.sleep(.01)
        assert sum(event['event'] == 'failure_hold_reminder' for event in events) == 1
        await resumed.restore_task('t-failure')
        await asyncio.sleep(.02)
        assert sum(event['event'] == 'failure_hold_reminder' for event in events) == 1
        assert (await self_check.failure_handling_checks(pool=pool))[0].status == self_check.OK
        for index in range(8):
            await pool.execute("UPDATE task_steps SET worker_id=$1,status='dispatched' WHERE id='s-failure'", f'warn-{index}')
            await resumed.handle_step_result(task_id='t-failure', step_id='s-failure', run_id=f'warn-{index}',
                role='worker', failure=Failure(FailureCause.REPORT), target={'kind': 'worker'}, result={})
        figures = await resumed.stats()
        assert figures['breakers_today'] == 7
        assert (await self_check.failure_handling_checks(pool=pool))[0].status == self_check.WARN
        await resumed.close()
    run_db(body, monkeypatch, timeout=30)


def test_existing_metrics_include_typed_recovery_only_in_act(monkeypatch):
    from looper.agents import failure_handling, metrics
    from looper.agents.failures import Failure, FailureCause, FailureTyping

    async def body(pool):
        await seed(pool)
        service = failure_handling.FailureHandlingService(pool=pool, mode=FailureTyping.ACT)
        monkeypatch.setattr(failure_handling, '_service', service)
        await service.handle_step_result(task_id='t-failure', step_id='s-failure', run_id='worker-1', role='worker',
            failure=Failure(FailureCause.REPORT), target={'kind': 'worker'}, result={})
        view = await metrics.compute(7, metrics.Filters(), pool=pool)
        assert view['failure_handling']['distribution'] == [{'role': 'worker', 'failure_class': 'judgment',
            'streak': 1, 'failures': 1, 'breakers': 0}]
        service.mode = FailureTyping.OFF
        assert 'failure_handling' not in await metrics.compute(7, metrics.Filters(), pool=pool)
        await service.close()
    run_db(body, monkeypatch, timeout=30)


def test_mode_exit_outbox_survives_until_the_planner_acknowledges_recovery(monkeypatch):
    from looper.agents import failure_handling
    from looper.agents.failures import Failure, FailureCause, FailureTyping
    from looper.db import step_failures, system_settings

    async def body(pool):
        await seed(pool)
        events = []
        async def publish(task_id, payload):
            events.append(payload)
        first = failure_handling.FailureHandlingService(pool=pool, publish=publish, mode=FailureTyping.ACT)
        handled = await first.handle_step_result(task_id='t-failure', step_id='s-failure', run_id='worker-1',
            role='worker', failure=Failure(FailureCause.LOGIN), target={'kind': 'worker'}, result={'error': 'fixture'})
        failure_id = handled.failure['id']
        await first.set_mode('off')
        assert len(await step_failures.pending_releases(pool=pool)) == 1
        assert await system_settings.get_settings(['engine.failure_pending_recovery'], pool=pool) == {
            'engine.failure_pending_recovery': 'true'}
        await first.close()
        resumed = failure_handling.FailureHandlingService(pool=pool, publish=publish, mode=FailureTyping.OFF)
        await resumed.prepare_restore()
        assert resumed.recovering
        await resumed.restore()
        releases = [item for item in events if item['event'] == 'failure_hold_released']
        assert len(releases) == 2 and releases[0]['targets'] == releases[1]['targets']
        await resumed.ack_release(failure_id)
        assert not resumed.recovering and not await step_failures.pending_releases(pool=pool)
        assert await system_settings.get_settings(['engine.failure_pending_recovery'], pool=pool) == {}
        await resumed.close()
    run_db(body, monkeypatch, timeout=30)


def test_disabling_admission_preserves_the_independent_finite_quota_wait(monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from looper.agents import admission, failure_handling
    from looper.agents.failures import Failure, FailureCause, FailureTyping

    async def body(pool):
        await seed(pool)
        await pool.execute("INSERT INTO llm_providers(id,name,vendor_type,auth) VALUES('quota-fixture','Quota fixture','custom','api_key')")
        service = failure_handling.FailureHandlingService(pool=pool, mode=FailureTyping.ACT)
        monkeypatch.setattr(admission, 'enabled', lambda: False)
        handled = await service.handle_step_result(task_id='t-failure', step_id='s-failure', run_id='worker-1',
            role='worker', failure=Failure(FailureCause.USAGE_LIMIT), provider_id='quota-fixture', target={'kind': 'worker'}, result={})
        scheduled = []
        monkeypatch.setattr(service, '_schedule', lambda hold: scheduled.append(hold))
        await service.recheck()
        assert scheduled[-1]['until'] == handled.hold['until']
        # An explicit account clear is different: the controller's OPEN state is authoritative.
        monkeypatch.setattr(admission, 'enabled', lambda: True)
        monkeypatch.setattr(admission, '_controller', SimpleNamespace(snapshot=AsyncMock(return_value={'state': 'open'})))
        await service.recheck()
        assert scheduled[-1]['until'] < handled.hold['until']
        await service.close()
    run_db(body, monkeypatch, timeout=30)
