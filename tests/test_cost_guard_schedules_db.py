"""Budget skips commit before their alert; rolled-back firings send nothing."""
import pytest

from looper.agents import cost_guard as cg
from looper.agents.cost_guard_settings import Config, Mode, RoleLimits
from tests.db_support import DSN
from tests.schedule_support import run_scheduled
from tests.subscription_support import cursor, started

pytestmark = pytest.mark.skipif(not DSN, reason='requires disposable LOOPER_TEST_DATABASE_URL')


@pytest.mark.parametrize('rollback', [False, True])
def test_subscription_role_cap_preserves_cursor_and_publishes_only_after_commit(monkeypatch, tmp_path, rollback):
    from looper.agents import cost_alerts

    async def check(env):
        sid = await started(env, target=None)
        before = await cursor(env, sid)
        guard = cg.Guard(Config(mode=Mode.ENFORCE, roles={
            'processor': RoleLimits(daily_usd=0, daily_equivalent_usd=0)}),
            day_start=env.scheduler.day_start, clock=env.clock, pool=env.pool)
        monkeypatch.setattr(cg, '_guard', guard)
        sent = []

        async def publish(notice):
            # This separate connection can see the skipped run only after the firing committed.
            rows = await env.pool.fetch('SELECT status FROM schedule_runs WHERE schedule_id=$1', sid)
            assert [row['status'] for row in rows] == ['skipped_budget']
            sent.append(notice)

        monkeypatch.setattr(cost_alerts, 'publish_skip', publish)
        if rollback:
            def abort(_schedule):
                raise RuntimeError('firing transaction rolled back')
            monkeypatch.setattr(env.scheduler, '_check_still_admitted', abort)
        await env.due(sid)
        await env.scheduler._fire_due(env.clock.now)
        assert await cursor(env, sid) == before
        rows = await env.pool.fetch('SELECT status, error FROM schedule_runs WHERE schedule_id=$1', sid)
        if rollback:
            assert rows == [] and sent == []
        else:
            assert len(rows) == 1 and rows[0]['status'] == 'skipped_budget'
            assert 'role_day processor' in rows[0]['error']
            assert len(sent) == 1 and sent[0].schedule_id == sid
    run_scheduled(check, monkeypatch, tmp_path)


@pytest.mark.parametrize('scope', ['role', 'global'])
@pytest.mark.parametrize('noted', [False, True])
def test_budget_denial_precedes_rejected_window_even_if_already_noted(monkeypatch, tmp_path, scope, noted):
    from datetime import timedelta
    from unittest.mock import AsyncMock
    from looper.agents import cost_alerts, usage_windows

    async def check(env):
        sid = await started(env, target=None)
        limits = ({'roles': {'processor': RoleLimits(daily_usd=0, daily_equivalent_usd=0)}}
                  if scope == 'role' else {'daily_usd': 0, 'daily_equivalent_usd': 0})
        guard = cg.Guard(Config(mode=Mode.ENFORCE, **limits), day_start=env.scheduler.day_start,
                         clock=env.clock, pool=env.pool)
        monkeypatch.setattr(cg, '_guard', guard)
        until = env.clock.now + timedelta(hours=1)
        hold = usage_windows.Hold(until, usage_windows.HoldReason.REJECTED, True, 'test_window')
        monkeypatch.setattr(guard, '_usage_hold', AsyncMock(return_value=(hold, 'synthetic-provider')))
        monkeypatch.setattr(cost_alerts, 'publish_skip', AsyncMock())
        monkeypatch.setattr(usage_windows, 'notify_hold', AsyncMock())
        usage_windows.clear_cache()
        if noted:
            usage_windows.note_schedule(sid, until=until, reason='rejected', reset_known=True, window='test_window')
        try:
            await env.due(sid)
            await env.scheduler._fire_due(env.clock.now)
            row = await env.pool.fetchrow('SELECT status,error FROM schedule_runs WHERE schedule_id=$1', sid)
            assert row['status'] == 'skipped_budget'
            assert ('role_day' if scope == 'role' else 'global_day') in row['error']
            cost_alerts.publish_skip.assert_awaited_once()
            usage_windows.notify_hold.assert_not_called()
        finally:
            usage_windows.clear_cache()
    run_scheduled(check, monkeypatch, tmp_path)
