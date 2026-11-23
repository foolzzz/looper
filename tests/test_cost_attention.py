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
