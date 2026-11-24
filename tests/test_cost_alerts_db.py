"""Claims survive a process restart; the previous day's repeat counters do not."""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from looper.agents import chat_settings, cost_alerts as alerts, cost_guard as cg, gate_service
from looper.agents.cost_guard_settings import Config, Mode
from tests.db_support import DSN, run_db

pytestmark = pytest.mark.skipif(not DSN, reason='LOOPER_TEST_DATABASE_URL is not set')


def test_restart_same_day_does_not_resend_and_old_claims_are_deleted(monkeypatch):
    day = datetime(2026, 11, 22, tzinfo=timezone.utc)
    guard = cg.Guard(Config(mode=Mode.ENFORCE), clock=lambda: day)
    guard.day = day
    monkeypatch.setattr(cg, '_guard', guard)
    monkeypatch.setattr(alerts, '_duplicates', {})
    monkeypatch.setattr(alerts, '_cleaned_day', None)
    monkeypatch.setattr(alerts, '_summary_day', None)
    monkeypatch.setattr(chat_settings, '_delivery_enabled', False)
    target = SimpleNamespace(channel_id='channel-telegram', target=SimpleNamespace(id='424242', group=False))
    monkeypatch.setattr(gate_service, 'dashboard_target', AsyncMock(return_value=target))
    sender = AsyncMock(return_value=(True, None))
    monkeypatch.setattr(alerts, '_sender', sender)

    async def body(pool):
        item = alerts.Alert(alerts.AlertKind.LIMIT, cg.Scope(cg.BudgetScope.ROLE_DAY, 'leads_check', 1),
                            cg.Currency.PROVIDER, day, run_kind=cg.RunKind.LEADS_CHECK)
        await alerts.flush((item,))
        alerts._duplicates.clear()  # A restart retains claims but loses the process counters.
        await alerts.flush((item,))
        assert sender.await_count == 1
        assert await pool.fetchval("SELECT count(*) FROM system_settings WHERE key LIKE 'cost_guard_alert:%'") == 1
        await pool.execute("INSERT INTO system_settings(key,value) VALUES('cost_guard_alert:stale',$1)",
                           (day - timedelta(days=3)).isoformat())
        await alerts.rollover(day + timedelta(days=1))
        assert sender.await_count == 2 and '昨天到上限之后又拦下' in sender.await_args.args[2]
        assert await pool.fetchval("SELECT value FROM system_settings WHERE key='cost_guard_alert:stale'") is None
        await alerts.rollover(day + timedelta(days=2))
        assert sender.await_count == 2
    run_db(body, monkeypatch)
