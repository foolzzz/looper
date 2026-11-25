"""Reserved rule execution binding survives the existing task configuration routes."""
from __future__ import annotations

import pytest

from looper.db import intent_rule_execution as ledger, projects, tasks
from tests.db_support import DSN, run_db, v1_api

pytestmark = pytest.mark.skipif(not DSN, reason='LOOPER_TEST_DATABASE_URL is not set')


async def ready(pool, *, bound):
    project = await projects.create_project(name='Lifecycle project', work_dir='/tmp/synthetic-review', pool=pool)
    await tasks.create_task(task_id='task-bound-review', requirement='Review verified MR', project_id=project['id'],
                            work_dir='/tmp/synthetic-review', status=tasks.READY,
                            config={'intent_rule_bound': True, 'old': 'value'} if bound else {'old': 'value'}, pool=pool)
    if bound:
        async with pool.acquire() as conn, conn.transaction():
            execution = await ledger.insert_execution(conn, task_id='task-bound-review', mode='manual', mr_key={
                'system_id': 'review-system', 'repository_id': 'repository', 'mr_id': '17', 'revision': 'head-a',
                'canonical_url': 'https://review.example/repository/mr/17'})
            await ledger.enqueue_start(conn, 'task-bound-review', execution['id'])
            await conn.execute("UPDATE intent_rule_executions SET revoke_state='requested' WHERE id=$1", execution['id'])


@pytest.mark.parametrize('route', ['db', 'configure'])
@pytest.mark.parametrize('replacement', [{}, {'intent_rule_bound': False, 'new': 'value'},
                                         {'intent_rule_bound': 'false', 'new': 'value'}])
def test_configuration_cannot_remove_a_committed_rule_binding(monkeypatch, tmp_path, route, replacement):
    async def check(pool):
        await ready(pool, bound=True)
        if route == 'db':
            assert await tasks.update_task_config('task-bound-review', config=replacement, pool=pool)
        else:
            async with v1_api(tmp_path, pool) as call:
                status, body = await call('tasks.configure', {'task_id': 'task-bound-review', 'config': replacement})
                assert status == 200
                assert body['result']['config']['intent_rule_bound'] is True
                status, body = await call('/api/v1/ops/task_control/start', {'task_id': 'task-bound-review'})
                assert status == 409 and body['error']['code'] == 'conflict'
        config = (await tasks.get_task('task-bound-review', pool=pool))['config']
        assert config == {**replacement, 'intent_rule_bound': True}
        assert await tasks.start_ready_task('task-bound-review', pool=pool) is None
        assert (await tasks.get_task('task-bound-review', pool=pool))['status'] == tasks.READY
        assert await pool.fetchval('SELECT revoke_state FROM intent_rule_executions') == 'requested'
    run_db(check, monkeypatch)


@pytest.mark.parametrize('route', ['db', 'configure'])
def test_unbound_configuration_keeps_the_existing_start_path(monkeypatch, tmp_path, route):
    async def check(pool):
        await ready(pool, bound=False)
        replacement = {'new': 'value'}
        if route == 'db':
            assert await tasks.update_task_config('task-bound-review', config=replacement, pool=pool)
        else:
            async with v1_api(tmp_path, pool) as call:
                status, body = await call('tasks.configure', {'task_id': 'task-bound-review', 'config': replacement})
                assert status == 200 and body['result']['config'] == replacement
        assert (await tasks.get_task('task-bound-review', pool=pool))['config'] == replacement
        # Existing task starts do not depend on any execution-ledger table.
        await pool.execute('ALTER TABLE intent_rule_executions RENAME TO hidden_rule_executions; '
                           'ALTER TABLE intent_rule_task_starts RENAME TO hidden_rule_starts')
        started = await tasks.start_ready_task('task-bound-review', pool=pool)
        assert started['status'] == 'pending'
        assert await tasks.start_ready_task('task-bound-review', pool=pool) is None
    run_db(check, monkeypatch)
