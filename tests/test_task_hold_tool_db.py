"""Operator-only hold controls use the same confirmations and service as HTTP clients."""
from tests.db_support import DSN, run_db, v1_api
import pytest

pytestmark = pytest.mark.skipif(not DSN, reason="requires disposable PostgreSQL")


def test_hold_release_is_confirmed_versioned_and_operator_only(tmp_path, monkeypatch):
    from looper.agents import failure_handling
    from looper.agents.failures import Failure, FailureCause, FailureTyping
    from looper.db import step_failures

    async def body(pool):
        await pool.execute("INSERT INTO projects(id,name) VALUES('p','Synthetic project')")
        await pool.execute("INSERT INTO tasks(id,project_id,requirement,status) VALUES('task','p','Synthetic work','executing')")
        await pool.execute("INSERT INTO task_steps(id,task_id,step_index,step_key,instruction,status,worker_id) "
                           "VALUES('step','task',0,'work','Do work','dispatched','worker-1')")
        events = []
        async def publish(task_id, event):
            events.append(event)
        service = failure_handling.FailureHandlingService(pool=pool, publish=publish, mode=FailureTyping.ACT)
        monkeypatch.setattr(failure_handling, "_service", service)
        try:
            handled = await service.handle_step_result(task_id='task', step_id='step', run_id='worker-1',
                role='worker', failure=Failure(FailureCause.LOGIN), target={'kind': 'worker'}, result={})
            failure_id = handled.failure['id']
            async with v1_api(tmp_path, pool) as call:
                status, result = await call('tasks.holds', {'task_id': 'task'}, client=True)
                assert status == 403
                status, result = await call('tasks.failure_history', {'task_id': 'task', 'limit': 1})
                assert status == 200 and result['result']['items'][0]['failure_cause'] == 'login'
                fields = {'task_id': 'task', 'step_id': 'step', 'failure_id': failure_id, 'action': 'retry'}
                status, result = await call('tasks.hold_release', fields, client=True)
                assert status == 403
                status, result = await call('tasks.hold_release', fields)
                assert status == 202 and result['status'] == 'needs_confirmation'
                assert len(await step_failures.list_holds('task', pool=pool)) == 1
                status, result = await call('confirm', {'confirmation_id': result['confirmation_id']})
                assert status == 200 and result['result']['failure_id'] == failure_id
                assert not await step_failures.list_holds('task', pool=pool)
                assert len([event for event in events if event['event'] == 'failure_hold_released']) == 1
                status, result = await call('tasks.hold_release', fields)
                assert status == 404 or status == 409
        finally:
            await service.close()
    run_db(body, monkeypatch, timeout=45)


def test_act_settings_confirm_and_drain_without_deleting_history(tmp_path, monkeypatch):
    from looper.agents import failure_handling
    from looper.agents.failures import FailureTyping
    from looper.db import system_settings

    async def body(pool):
        service = failure_handling.FailureHandlingService(pool=pool)
        monkeypatch.setattr(failure_handling, '_service', service)
        try:
            async with v1_api(tmp_path, pool) as call:
                status, result = await call('engine_settings.update', {'failure_typing': 'act'})
                assert status == 202
                assert await system_settings.get_settings(['engine.failure_typing'], pool=pool) == {}
                status, saved = await call('confirm', {'confirmation_id': result['confirmation_id']})
                assert status == 200, saved
                assert saved['result']['failure_typing']['value'] == 'act'
                assert service.mode is FailureTyping.ACT
                status, result = await call('engine_settings.update', {'failure_typing': None})
                assert status == 202
                status, saved = await call('confirm', {'confirmation_id': result['confirmation_id']})
                assert status == 200 and saved['result']['failure_typing']['source'] == 'built-in'
                assert service.mode is FailureTyping.OFF
        finally:
            await service.close()
    run_db(body, monkeypatch, timeout=45)


def test_hold_tool_catalog_authorization_confirmation_and_mode_race(tmp_path, monkeypatch):
    from looper.agents import failure_handling
    from looper.agents.failures import FailureTyping
    from looper.agents.ops import Turn
    from looper.agents.ops.service import ManagerOps, Platform
    from looper.agents.ops.settings import TOOLS as SETTINGS_TOOLS

    async def body(pool):
        service = failure_handling.FailureHandlingService(pool=pool)
        monkeypatch.setattr(failure_handling, '_service', service)
        ops = ManagerOps(Platform(bus=None, task_router=None), url='http://127.0.0.1:9', token='synthetic-test-token')
        own = Turn(channel='web', chat='console', message_id='message')
        stranger = Turn(channel='web', chat='api', message_id='message', client_id='synthetic-client')
        try:
            for mode in (FailureTyping.OFF, FailureTyping.RECORD):
                await service.set_mode(mode)
                listing = {entry['name']: entry for entry in await ops.current_listing(own)}
                assert 'looper_task_hold' not in listing
                assert listing['looper_settings'] == SETTINGS_TOOLS[0].listing()
            await service.set_mode(FailureTyping.ACT)
            listing = {entry['name']: entry for entry in await ops.current_listing(own)}
            assert 'looper_task_hold' in listing
            assert 'engine' in listing['looper_settings']['inputSchema']['properties']['action']['enum']
            assert 'looper_task_hold' not in {entry['name'] for entry in await ops.current_listing(stranger)}
            await service.set_mode(None)
            listing = {entry['name']: entry for entry in await ops.current_listing(own)}
            assert 'looper_task_hold' not in listing
            assert listing['looper_settings'] == SETTINGS_TOOLS[0].listing()
            stale = await ops.call('looper_task_hold', {'action': 'view', 'task_id': 'task'})
            assert stale['status'] == 'error' and stale['error']['code'] == 'forbidden'
        finally:
            await service.close()
    run_db(body, monkeypatch, timeout=45)
