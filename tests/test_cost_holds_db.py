"""Spend holds persist one decision per scope and retain task waiters across restarts."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from looper.agents import cost_holds
from looper.db import cost_holds as store
from tests.db_support import DSN, run_db, v1_api

pytestmark = pytest.mark.skipif(not DSN, reason='LOOPER_TEST_DATABASE_URL is not set')


@pytest.fixture(autouse=True)
def isolated_guard(monkeypatch):
    from looper.agents import cost_guard, cost_guard_settings

    monkeypatch.setattr(cost_guard, '_guard', cost_guard.Guard(cost_guard_settings.Config()))


async def seed(pool):
    await pool.execute("INSERT INTO projects(id,name,task_budget_usd) VALUES ('p-cost','Cost',2)")
    await pool.execute("INSERT INTO tasks(id,project_id,requirement,status) VALUES "
                       "('t-cost','p-cost','Synthetic task','awaiting_budget'),"
                       "('t-cost-two','p-cost','Another synthetic task','awaiting_budget')")


def test_concurrent_scope_creation_one_hold_and_multiple_waiters(monkeypatch):
    async def body(pool):
        await seed(pool)
        rows = await asyncio.gather(*(store.create_hold('global_day', 'global', 'provider_priced_usd', 2, 2,
            'budget', task_id=task, pool=pool) for task in ('t-cost', 't-cost-two')))
        assert rows[0][0]['id'] == rows[1][0]['id']
        assert sum(created for _, created in rows) == 1
        assert len(await store.list_pending(pool=pool)) == 1
        assert set(await store.task_ids(rows[0][0]['id'], pool=pool)) == {'t-cost', 't-cost-two'}
    run_db(body, monkeypatch, timeout=30)


def test_continue_raises_task_cap_and_wakes_once_without_ending_task(monkeypatch):
    async def body(pool):
        await seed(pool)
        row, _ = await store.create_hold('task', 't-cost', 'provider_priced_usd', 2, 2, 'budget',
                                         task_id='t-cost', pool=pool)
        platform = SimpleNamespace(bus=SimpleNamespace(publish=AsyncMock()))
        with pytest.raises(ValueError, match='上限'):
            await cost_holds.decide(row['id'], 'continue', via='api', platform=platform, pool=pool)
        result = await cost_holds.decide(row['id'], 'continue', 4, via='dashboard', platform=platform, pool=pool)
        assert result['state'] == 'continued' and result['decided_via'] == 'dashboard'
        assert await pool.fetchval("SELECT budget_usd FROM tasks WHERE id='t-cost'") == 4
        assert await pool.fetchval("SELECT status FROM tasks WHERE id='t-cost'") == 'running'
        assert await pool.fetchval("SELECT count(*) FROM task_outcomes") == 0
        assert platform.bus.publish.await_count == 1
        with pytest.raises(ValueError, match='处理'):
            await cost_holds.decide(row['id'], 'continue', 5, via='api', platform=platform, pool=pool)
        assert platform.bus.publish.await_count == 1
    run_db(body, monkeypatch, timeout=30)


def test_expiry_and_stop_follow_task_stop_path(monkeypatch):
    async def body(pool):
        await seed(pool)
        row, _ = await store.create_hold('task', 't-cost', 'provider_priced_usd', 2, 2, 'budget',
                                         task_id='t-cost', pool=pool)
        await pool.execute("UPDATE cost_holds SET created_at=now()-interval '25 hours' WHERE id=$1", row['id'])
        platform = SimpleNamespace(bus=SimpleNamespace(publish=AsyncMock()))
        expired = await cost_holds.expire(platform=platform, pool=pool)
        assert expired[0]['state'] == 'expired'
        message = platform.bus.publish.call_args.args[0]
        assert message.message_type.value == 'task_stop'
        assert message.payload['stop_note'] == 'stopped at a spend hold'
        assert await cost_holds.expire(platform=platform, pool=pool) == []
    run_db(body, monkeypatch, timeout=30)


def test_holds_operations_reject_api_clients(monkeypatch, tmp_path):
    async def body(pool):
        async with v1_api(tmp_path, pool) as call:
            status, _ = await call('cost_holds.list', client=True)
            assert status == 403
            status, _ = await call('cost_holds.decide', {'hold_id': 1, 'decision': 'stop'}, client=True)
            assert status == 403
            status, answer = await call('cost_holds.list')
            assert status == 200 and answer['result'] == {'holds': []}
    run_db(body, monkeypatch, timeout=30)


def test_reused_hold_sends_one_question_and_retains_waiting_tasks(monkeypatch):
    async def body(pool):
        await seed(pool)
        question = AsyncMock()
        monkeypatch.setattr(cost_holds, '_question', question)
        platform = SimpleNamespace(task_router=SimpleNamespace(_tasks={
            't-cost': SimpleNamespace(status='running'), 't-cost-two': SimpleNamespace(status='running')}))
        first, created = await cost_holds.create_hold('global_day', 'global', 'provider_priced_usd', 2, 2,
            task_id='t-cost', platform=platform, pool=pool)
        second, created_again = await cost_holds.create_hold('global_day', 'global', 'provider_priced_usd', 2, 2,
            task_id='t-cost-two', platform=platform, pool=pool)
        assert created and not created_again and first['id'] == second['id']
        question.assert_awaited_once()
        assert all(task.status == 'awaiting_budget' for task in platform.task_router._tasks.values())
        assert await pool.fetchval("SELECT count(*) FROM tasks WHERE status='awaiting_budget'") == 2
        # A newly constructed store reads the same pending hold and all its waiters.
        pending = await store.list_pending(pool=pool)
        assert pending[0]['id'] == first['id'] and set(pending[0]['task_ids']) == {'t-cost', 't-cost-two'}
    run_db(body, monkeypatch, timeout=30)


@pytest.mark.parametrize('decision', ['continue', 'stop'])
def test_decided_hold_retries_a_lost_wakeup_on_the_refresh_tick(monkeypatch, decision):
    async def body(pool):
        await seed(pool)
        row, _ = await store.create_hold('task', 't-cost', 'provider_priced_usd', 2, 2, 'budget',
                                         task_id='t-cost', pool=pool)
        publish = AsyncMock(side_effect=RuntimeError('synthetic disconnected bus'))
        platform = SimpleNamespace(bus=SimpleNamespace(publish=publish))
        result = await cost_holds.decide(row['id'], decision, 4 if decision == 'continue' else None,
                                        via='api', platform=platform, pool=pool)
        assert result['state'] == ('continued' if decision == 'continue' else 'stopped')
        assert await pool.fetchval('SELECT applied_at FROM cost_holds WHERE id=$1', row['id']) is None
        publish.side_effect = None
        await cost_holds.expire(platform=platform, pool=pool)
        assert await pool.fetchval('SELECT applied_at FROM cost_holds WHERE id=$1', row['id']) is not None
        assert publish.await_count == 2
        await cost_holds.expire(platform=platform, pool=pool)
        assert publish.await_count == 2
    run_db(body, monkeypatch, timeout=30)


def test_upgrade_adds_ask_to_existing_project_constraint(monkeypatch):
    async def body(pool):
        from looper.db import connection
        await pool.execute("ALTER TABLE projects DROP CONSTRAINT projects_task_budget_action_check, "
                           "ADD CONSTRAINT projects_task_budget_action_check "
                           "CHECK (task_budget_action IN ('wrap_up','fail','defer'))")
        await pool.execute("INSERT INTO projects(id,name,task_budget_action) VALUES ('p-defer','Deferred','defer')")
        # A fresh database has no per-file marker. Its existing hold table cannot conceal the missing value.
        async with pool.acquire() as conn:
            assert await connection.schema_change_pending(conn)
        await connection.ensure_schema(pool)
        await pool.execute("INSERT INTO projects(id,name,task_budget_action) VALUES ('p-ask','Ask','ask')")
        assert await pool.fetchval("SELECT task_budget_action FROM projects WHERE id='p-defer'") == 'defer'
        assert await pool.fetchval("SELECT convalidated FROM pg_constraint WHERE conrelid='projects'::regclass "
                                   "AND conname='projects_task_budget_action_check'")
        async with pool.acquire() as conn:
            assert not await connection.schema_change_pending(conn)
    run_db(body, monkeypatch, timeout=30)


def test_continue_without_amount_must_cover_the_launch_estimate(monkeypatch):
    async def body(pool):
        await seed(pool)
        row, _ = await store.create_hold('task', 't-cost', 'provider_priced_usd', 2, 1.8, 'budget',
                                         task_id='t-cost', estimate_usd=1, pool=pool)
        with pytest.raises(ValueError, match='金额'):
            await cost_holds.decide(row['id'], 'continue', via='lark', pool=pool)
        assert (await store.get(row['id'], pool=pool))['state'] == 'pending'
        with pytest.raises(ValueError, match='预估'):
            await cost_holds.decide(row['id'], 'continue', 2.5, via='api', pool=pool)
        result = await cost_holds.decide(row['id'], 'continue', 2.8, via='api', pool=pool)
        assert result['state'] == 'continued'
    run_db(body, monkeypatch, timeout=30)


def test_raising_a_hold_limit_requires_operator_confirmation(monkeypatch, tmp_path):
    async def body(pool):
        await seed(pool)
        row, _ = await store.create_hold('task','t-cost','provider_priced_usd',2,2,'budget',task_id='t-cost',pool=pool)
        async with v1_api(tmp_path,pool) as call:
            status, pending = await call('cost_holds.decide', {'hold_id':row['id'],'decision':'continue', 'new_limit_usd':4})
            assert status == 202 and pending['status']=='needs_confirmation'
            assert '$4' in pending['impact']
            assert (await store.get(row['id'],pool=pool))['state']=='pending'
            assert await pool.fetchval("SELECT budget_usd FROM tasks WHERE id='t-cost'") is None
            status, decided = await call('confirm', {'confirmation_id': pending['confirmation_id']})
            assert status == 200 and decided['result']['state']=='continued'
            assert await pool.fetchval("SELECT budget_usd FROM tasks WHERE id='t-cost'")==4
    run_db(body,monkeypatch,timeout=30)


def test_hold_question_uses_blocking_delivery_and_one_channel_notification(monkeypatch):
    from looper.agents import chat_settings, gate_service

    async def body(pool):
        await seed(pool)
        sender=AsyncMock(return_value=(True,None))
        monkeypatch.setattr(cost_holds,'_sender',sender)
        monkeypatch.setattr(cost_holds,'_dashboard_url','https://dashboard.example.test')
        monkeypatch.setattr(chat_settings,'delivery_enabled',lambda:True)
        monkeypatch.setattr(gate_service,'dashboard_target',AsyncMock(return_value=SimpleNamespace(
            channel_id='channel-web',target=SimpleNamespace(id='synthetic-operator',group=False))))
        first,_=await cost_holds.create_hold('task','t-cost','provider_priced_usd',2,2,task_id='t-cost',pool=pool)
        await cost_holds.create_hold('task','t-cost','provider_priced_usd',2,2,task_id='t-cost',pool=pool)
        sender.assert_awaited_once()
        question=sender.await_args.args[2]
        assert f"继续 C-{first['id']}" in question and f"停 C-{first['id']}" in question
        assert 'https://dashboard.example.test/#todo' in question
        row=await pool.fetchrow('SELECT urgency,decision,reason FROM proactive_deliveries WHERE dedupe_key=$1',
                                f"cost-hold:{first['id']}")
        assert dict(row)=={'urgency':'blocking','decision':'send','reason':'urgent'}
    run_db(body,monkeypatch,timeout=30)


def test_expired_hold_cannot_change_an_already_cancelled_task(monkeypatch):
    async def body(pool):
        await seed(pool)
        row,_=await store.create_hold('task','t-cost','provider_priced_usd',2,2,'budget',task_id='t-cost',pool=pool)
        await pool.execute("UPDATE tasks SET status='cancelled' WHERE id='t-cost'")
        await pool.execute("UPDATE cost_holds SET created_at=now()-interval '25 hours' WHERE id=$1",row['id'])
        publish=AsyncMock()
        await cost_holds.expire(platform=SimpleNamespace(bus=SimpleNamespace(publish=publish)),pool=pool)
        assert await pool.fetchval("SELECT status FROM tasks WHERE id='t-cost'")=='cancelled'
        publish.assert_not_awaited()
        assert (await store.get(row['id'],pool=pool))['state']=='expired'
    run_db(body,monkeypatch,timeout=30)


def test_one_scope_cannot_resume_a_task_still_waiting_on_another_scope(monkeypatch):
    async def body(pool):
        await seed(pool)
        await pool.execute("INSERT INTO system_settings(key,value) VALUES ('cost_guard.daily_usd','2')")
        first,_=await store.create_hold('task','t-cost','provider_priced_usd',2,2,'budget',task_id='t-cost',pool=pool)
        second,_=await store.create_hold('global_day','global','provider_priced_usd',2,2,'budget',task_id='t-cost',pool=pool)
        publish=AsyncMock()
        platform=SimpleNamespace(bus=SimpleNamespace(publish=publish))
        await cost_holds.decide(first['id'],'continue',4,via='api',platform=platform,pool=pool)
        assert await pool.fetchval("SELECT status FROM tasks WHERE id='t-cost'")=='awaiting_budget'
        publish.assert_not_awaited()
        await cost_holds.decide(second['id'],'continue',4,via='api',platform=platform,pool=pool)
        assert await pool.fetchval("SELECT status FROM tasks WHERE id='t-cost'")=='running'
        publish.assert_awaited_once()
    run_db(body,monkeypatch,timeout=30)


@pytest.mark.parametrize(('currency', 'cap'), [('provider_priced_usd', 'daily_usd'),
                                              ('equivalent_usd', 'daily_equivalent_usd')])
def test_review_budget_continue_raises_only_the_role_currency_cap(monkeypatch, currency, cap):
    async def body(pool):
        key = f'cost_guard.roles.outbound_check.{cap}'
        await pool.execute("INSERT INTO system_settings(key,value) VALUES ($1,'2')", key)
        row, _ = await store.create_hold('role_day', 'outbound_check', currency, 2, 2, 'review_budget',
                                         estimate_usd=0.1, pool=pool)
        with pytest.raises(ValueError, match='上限'):
            await cost_holds.decide(row['id'], 'continue', 2.05, via='api', pool=pool)
        decided = await cost_holds.decide(row['id'], 'continue', 3, via='api', pool=pool)
        assert decided['state'] == 'continued' and decided['task_ids'] == []
        assert await pool.fetchval('SELECT value FROM system_settings WHERE key=$1', key) == '3.0'
        assert await pool.fetchval("SELECT count(*) FROM system_settings WHERE key LIKE 'cost_guard.daily_%'") == 0
    run_db(body, monkeypatch, timeout=30)


def test_review_budget_question_explains_that_the_blocked_action_will_not_be_retried(monkeypatch):
    from looper.agents import chat_settings, gate_service
    async def body(pool):
        sender = AsyncMock(return_value=(True, None))
        monkeypatch.setattr(cost_holds, '_sender', sender)
        monkeypatch.setattr(chat_settings, 'delivery_enabled', lambda: False)
        monkeypatch.setattr(gate_service, 'dashboard_target', AsyncMock(return_value=SimpleNamespace(
            channel_id='synthetic-channel', target=SimpleNamespace(id='synthetic-chat', group=False))))
        row, _ = await store.create_hold('role_day', 'outbound_check', 'provider_priced_usd', 2, 2,
                                         'review_budget', pool=pool)
        await cost_holds._question(row)
        text = sender.await_args.args[2]
        assert '对外审查今天的预算用完了，要提高上限吗？被拦下的动作不会自动重发。' in text
        assert f'继续 C-{row["id"]}' in text and f'停 C-{row["id"]}' in text
        assert '24 小时未回答，上限不变' in text
    run_db(body, monkeypatch, timeout=30)
