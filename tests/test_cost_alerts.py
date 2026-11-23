"""Spend alert delivery, durable daily claims and the in-memory duplicate summary."""
import asyncio
from dataclasses import FrozenInstanceError, replace
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from looper.agents import chat_settings, cost_alerts as alerts, cost_guard as cg, gate_service
from looper.agents import proactive_delivery as delivery
from looper.agents.cost_guard_settings import Config, Mode

DAY = datetime(2026, 11, 22, tzinfo=timezone.utc)


def setup(monkeypatch, mode=Mode.ENFORCE):
    guard = cg.Guard(Config(mode=mode), clock=lambda: DAY)
    guard.day = DAY
    monkeypatch.setattr(cg, '_guard', guard)
    monkeypatch.setattr(alerts, '_duplicates', {})
    monkeypatch.setattr(alerts, '_cleaned_day', None)
    monkeypatch.setattr(alerts, '_summary_day', None)
    monkeypatch.setattr(chat_settings, '_delivery_enabled', False)
    monkeypatch.setattr(chat_settings, '_metrics_enabled', False)
    target = SimpleNamespace(channel_id='channel-telegram', target=SimpleNamespace(id='424242', group=False))
    monkeypatch.setattr(gate_service, 'dashboard_target', AsyncMock(return_value=target))
    sender = AsyncMock(return_value=(True, None))
    monkeypatch.setattr(alerts, '_sender', sender)
    monkeypatch.setattr(alerts, '_dashboard_url', 'https://dashboard.example')
    return guard, sender


def alert(kind=alerts.AlertKind.LIMIT, *, day=DAY, ref='leads_check', currency=cg.Currency.PROVIDER):
    return alerts.Alert(kind, cg.Scope(cg.BudgetScope.ROLE_DAY, ref, 1), currency, day,
                        spent_usd=.9, run_kind=cg.RunKind.LEADS_CHECK)


def test_alert_is_frozen_and_claim_losers_only_count(monkeypatch):
    _, sender = setup(monkeypatch)
    claim = AsyncMock(side_effect=[True, False, False])
    monkeypatch.setattr(alerts.system_settings, 'claim', claim)
    item = alert()
    with pytest.raises(FrozenInstanceError):
        item.spent_usd = 2

    async def check():
        await alerts.flush((item, item, item))
        assert sender.await_count == 1
        assert claim.await_args.args == ('cost_guard_alert:limit:role_day:leads_check:provider_priced_usd', DAY.isoformat())
        assert list(alerts._duplicates.values()) == [2]
        text = sender.await_args.args[2]
        assert '后台角色 leads_check 今天不再运行' in text
        assert text.endswith('https://dashboard.example/#settings/cost')
    asyncio.run(check())


@pytest.mark.parametrize('mode', [Mode.OFF, Mode.OBSERVE])
def test_inactive_modes_send_and_claim_nothing(monkeypatch, mode):
    _, sender = setup(monkeypatch, mode)
    claim = AsyncMock(side_effect=AssertionError('inactive alert wrote a claim'))
    monkeypatch.setattr(alerts.system_settings, 'claim', claim)
    asyncio.run(alerts.flush((alert(),)))
    sender.assert_not_awaited()
    claim.assert_not_awaited()
    assert alerts._duplicates == {}


def test_near_limit_uses_currency_and_sanitizes_names(monkeypatch):
    _, sender = setup(monkeypatch)
    monkeypatch.setattr(alerts.system_settings, 'claim', AsyncMock(return_value=True))
    item = alert(alerts.AlertKind.NEAR_LIMIT, ref='processor\n\u202e', currency=cg.Currency.EQUIVALENT)
    asyncio.run(alerts.flush((item,)))
    text = sender.await_args.args[2]
    assert '花费提醒：后台角色 processor 今天的等价花费已花及预留 $0.9' in text
    assert '上限 $1 的 80%' in text and '\u202e' not in text


def test_schedule_skip_names_schedule_and_flushes_only_after_publication(monkeypatch):
    _, sender = setup(monkeypatch)
    monkeypatch.setattr(alerts.system_settings, 'claim', AsyncMock(return_value=True))
    monkeypatch.setattr(alerts.db_schedules, 'get_schedule', AsyncMock(return_value={'name': 'News\nfeed\u202e'}))
    item = replace(alert(), run_kind=cg.RunKind.SCHEDULE, schedule_id='schedule-synthetic')
    notice = alerts.BudgetSkipped('schedule-synthetic', item.scope, 1, .9, alerts=(item,))
    sender.assert_not_awaited()
    asyncio.run(alerts.publish_skip(notice))
    assert '定时任务『News feed』这次触发已跳过' in sender.await_args.args[2]


def test_guard_error_has_one_global_key(monkeypatch):
    _, sender = setup(monkeypatch)
    claim = AsyncMock(return_value=True)
    monkeypatch.setattr(alerts.system_settings, 'claim', claim)
    item = alerts.Alert(alerts.AlertKind.GUARD_ERROR, cg.Scope(cg.BudgetScope.GLOBAL_DAY, 'global'), None, DAY)
    asyncio.run(alerts.flush((item,)))
    assert claim.await_args.args[0] == 'cost_guard_alert:guard_error:global_day:global:none'
    assert '定时触发检查失败时跳过本次触发' in sender.await_args.args[2]


def test_rollover_sends_one_summary_and_cleans_old_claims(monkeypatch):
    _, sender = setup(monkeypatch)
    monkeypatch.setattr(alerts.system_settings, 'claim', AsyncMock(return_value=False))
    pool = SimpleNamespace(execute=AsyncMock())
    monkeypatch.setattr(alerts, 'get_pool', lambda: pool)

    async def check():
        await alerts.flush((alert(), alert(), alert(ref='processor')))
        await alerts.rollover(DAY + timedelta(days=1))
        assert sender.await_count == 1
        assert sender.await_args.args[2].startswith('昨天到上限之后又拦下：')
        assert 'leads_check' in sender.await_args.args[2] and '2 次' in sender.await_args.args[2]
        assert alerts._duplicates == {}
        sql, cutoff = pool.execute.await_args.args
        assert "key LIKE 'cost_guard_alert:%'" in sql and 'value < $1' in sql
        assert cutoff == (DAY - timedelta(days=1)).isoformat()
        await alerts.rollover(DAY + timedelta(days=1))
        assert sender.await_count == 1
    asyncio.run(check())


def test_quiet_hours_decision_defers_alert_and_claim_failure_is_nonfatal(monkeypatch):
    _, sender = setup(monkeypatch)
    monkeypatch.setattr(chat_settings, '_delivery_enabled', True)
    monkeypatch.setattr(alerts.system_settings, 'claim', AsyncMock(return_value=True))
    decision = AsyncMock(return_value=delivery.Decision(delivery.Outcome.DIGEST, delivery.Reason.QUIET_HOURS))
    monkeypatch.setattr(delivery, 'decide', decision)
    asyncio.run(alerts.flush((alert(),)))
    sender.assert_not_awaited()
    sent = decision.await_args.args[0]
    assert sent.kind is delivery.DeliveryKind.ADVISOR_ALERT and sent.urgency is delivery.Urgency.NORMAL
    monkeypatch.setattr(alerts.system_settings, 'claim', AsyncMock(side_effect=RuntimeError('offline')))
    asyncio.run(alerts.flush((alert(),)))
    sender.assert_not_awaited()


def test_late_previous_day_claim_loser_does_not_send_a_second_summary(monkeypatch):
    _, sender = setup(monkeypatch)
    monkeypatch.setattr(alerts.system_settings, 'claim', AsyncMock(return_value=False))
    monkeypatch.setattr(alerts, 'get_pool', lambda: SimpleNamespace(execute=AsyncMock()))

    async def check():
        item = alert()
        await alerts.flush((item,))
        claiming, release = asyncio.Event(), asyncio.Event()

        async def delayed_claim(*_args):
            claiming.set()
            await release.wait()
            return False

        monkeypatch.setattr(alerts.system_settings, 'claim', delayed_claim)
        late = asyncio.create_task(alerts.flush((item,)))
        await claiming.wait()
        tomorrow = DAY + timedelta(days=1)
        await alerts.rollover(tomorrow)
        assert sender.await_count == 1
        release.set()
        await late
        await alerts.rollover(tomorrow)
        assert sender.await_count == 1
        assert alerts._duplicates == {}
    asyncio.run(check())
