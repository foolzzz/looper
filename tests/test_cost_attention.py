"""Spend holds are operator attention, never an extra observation-mode read."""
import asyncio
from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest

from looper.agents import attention, cost_guard_settings
from looper.db import cost_holds, tasks


def test_spend_wait_is_an_active_needs_you_task():
    assert tasks.task_group("awaiting_budget") is tasks.TaskGroup.NEEDS_YOU
    assert "awaiting_budget" in tasks.ACTIVE_STATUSES
    assert "awaiting_budget" not in tasks.RESTARTABLE_STATUSES


@pytest.mark.parametrize("mode", [cost_guard_settings.Mode.OFF, cost_guard_settings.Mode.OBSERVE])
def test_disabled_cost_attention_never_reads_holds(monkeypatch, mode):
    monkeypatch.setattr(cost_guard_settings, "_config", cost_guard_settings.Config(mode=mode))
    read = AsyncMock(side_effect=AssertionError("must not read"))
    monkeypatch.setattr(cost_holds, "list_pending", read)
    assert asyncio.run(attention._cost(None)) is attention.NotRead.OFF
    read.assert_not_called()


def test_cost_attention_lists_the_question_and_exact_reply_grammar(monkeypatch):
    monkeypatch.setattr(cost_guard_settings, "_config", cost_guard_settings.Config(mode=cost_guard_settings.Mode.ENFORCE))
    now = datetime.now(timezone.utc)
    monkeypatch.setattr(cost_holds, "list_pending", AsyncMock(return_value=[{
        "id": 7, "scope": "task", "scope_ref": "demo", "currency": "provider_priced_usd", "limit_usd": 2,
        "spent_usd": 2, "reason": "budget", "state": "pending", "created_at": now,
    }]))
    items, count = asyncio.run(attention._cost(None))
    assert count == 1 and items[0]["ref"] == "C-7" and items[0]["href"] == "#settings/cost"
    assert asyncio.run(attention.earliest("cost")) == items[0]
    text = attention.summary_text({"total": 1, "sections": [{"kind": "cost", "state": "ok", "count": count,
                                   "items": items, "error": None}]})
    assert "$2.00" in text and "继续 C-7" in text and "停 C-7" in text


def test_unanswered_attention_has_no_money_and_keeps_the_schedule_title():
    paused_at = datetime(2026, 11, 20, tzinfo=timezone.utc)
    item = attention._cost_item({"id": 4, "scope": "schedule", "scope_ref": "sched", "schedule_name": "Brief",
                                  "pushes": 15, "reason": "unanswered", "created_at": paused_at,
                                  "currency": None, "limit_usd": None, "spent_usd": None})
    assert item["title"] == "『Brief』15 条推送没人回"
    assert item["facts"] == {"reason": "unanswered", "pushes": 15}
    text = attention.summary_text({"total": 1, "sections": [{"kind": "cost", "state": "ok", "count": 1,
                                   "items": [item], "error": None}]})
    assert "C-4" in text and "继续 C-4" in text and "$" not in text
    schedule = attention._schedule_items([{
        "id": "sched", "name": "Brief", "project_name": "Demo", "consecutive_failures": 0,
        "failures_alert_at": None, "stale_alert_at": None, "deadline_alert_at": None, "overlap_alert_at": None,
        "last_status": None, "last_ok_at": None, "pause_reason": "unanswered", "paused_at": paused_at,
    }])[0]
    assert schedule["facts"]["alerts"] == ["unanswered"]
    assert schedule["key"] == "schedule:sched"
    assert attention._ALERTS["unanswered"] == "没人回，已自动暂停"
    assert attention._TITLES["schedules"] == "失败的定时任务"
    operator = attention._schedule_items([{
        **{"id": "sched-op", "name": "Other", "project_name": "Demo", "consecutive_failures": 0,
           "failures_alert_at": None, "stale_alert_at": None, "deadline_alert_at": None, "overlap_alert_at": None,
           "last_status": None, "last_ok_at": None, "paused_at": paused_at},
        "pause_reason": "operator",
    }])[0]
    assert "unanswered" not in operator["facts"]["alerts"]
