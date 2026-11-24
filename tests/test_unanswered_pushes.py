"""The unanswered-push switch is independent of cost-guard mode and stays quiet while metrics are off."""

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

from looper.agents import chat_settings, cost_guard, cost_holds, unanswered_pushes
from looper.agents.cost_guard import BudgetScope
from looper.agents.cost_guard_settings import Config, UnansweredRule
from looper.db import connection


def test_hold_scope_matches_budget_scope_and_adds_the_whole_schedule():
    assert [scope.value for scope in list(cost_holds.HoldScope)[:4]] == [scope.value for scope in BudgetScope]
    assert cost_holds.HoldScope.SCHEDULE.value == "schedule"
    assert [reason.value for reason in unanswered_pushes.PauseReason] == ["operator", "unanswered", "budget", "quota"]


def test_metrics_off_does_not_ask_or_pause_and_warns_once_a_day(monkeypatch, caplog):
    monkeypatch.setattr("looper.agents.cost_guard_settings._config",
                        Config(unanswered=UnansweredRule(enabled=True)))
    monkeypatch.setattr(unanswered_pushes, "_last_checked", None)
    monkeypatch.setattr(unanswered_pushes, "_warned_day", None)
    chat_settings.set_metrics_enabled(False)

    def queried():
        raise AssertionError("check queried the database")

    monkeypatch.setattr(connection, "get_pool", queried)
    now = datetime(2026, 11, 23, 12, tzinfo=timezone.utc)
    with caplog.at_level("WARNING"):
        asyncio.run(unanswered_pushes.check(now))
        asyncio.run(unanswered_pushes.check(now + timedelta(minutes=30)))
        asyncio.run(unanswered_pushes.check(now + timedelta(days=1)))
    warnings = [record.message for record in caplog.records if "manager.metrics.enabled" in record.message]
    assert warnings == [
        "没人回的推送规则已打开，但 manager.metrics.enabled 是关的：没有推送和回复记录，本次不提问也不暂停",
        "没人回的推送规则已打开，但 manager.metrics.enabled 是关的：没有推送和回复记录，本次不提问也不暂停",
    ]
    chat_settings.set_metrics_enabled(False)


def test_scheduler_tick_checks_unanswered_pushes_after_the_day_refresh(monkeypatch):
    from looper.agents.scheduler import Scheduler
    from looper.db import schedules as db_schedules

    order = []

    async def refresh():
        order.append("refresh")

    async def check(now):
        order.append(now)

    monkeypatch.setattr(cost_guard, "refresh_day", refresh)
    monkeypatch.setattr(unanswered_pushes, "check", check)
    moment = datetime(2026, 11, 23, 12, tzinfo=timezone.utc)
    scheduler = Scheduler(bus=SimpleNamespace(), clock=lambda: moment)
    monkeypatch.setattr(scheduler, "_settle_running", AsyncMock())
    monkeypatch.setattr(scheduler, "_fire_due", AsyncMock())
    monkeypatch.setattr(scheduler._notifier, "check_stale", AsyncMock())
    monkeypatch.setattr(scheduler._notifier, "check_deadlines", AsyncMock())
    monkeypatch.setattr(db_schedules, "schedules_with_queued_runs", AsyncMock(return_value=[]))
    asyncio.run(scheduler.tick())
    assert order == ["refresh", moment]
