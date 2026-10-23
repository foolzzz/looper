"""A schedule request is refused with its reason before anything is written: each field is checked on its own, so
these run without a database. What needs the database (a profile or a notify target the registry must know) is in
``test_schedules_db`` and ``test_schedule_gate_budget_alerts_db``."""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock

import pytest

from looper.agents import scheduler_settings, schedules
from looper.agents.projects import HTTP_400, RequestError


@pytest.mark.parametrize(("body", "error"), [
    ({"name": ""}, "a schedule name is required"),
    ({"name": "x" * 81}, "at most 80 characters"),
    ({"cron": "*/3 * * * *"}, "fires 3 minutes apart"),
    ({"cron": "0 9 * *"}, "5 fields"),
    ({"cron": "0 0 30 2 *"}, "never fires"),
    ({"timezone": "Mars/Base"}, "unknown time zone"),
    ({"requirement": "   "}, "requirement is required"),
    ({"requirement": "x" * 27_001}, "at most 27000 characters"),
    ({"overlap": "both"}, "overlap must be one of skip, queue, parallel"),
    ({"catch_up": "all"}, "catch_up must be one of once, skip"),
    ({"notify": "sometimes"}, "notify must be one of failures, alerts, always, never"),
    ({"overlap": "parallel", "max_parallel_runs": 0}, "max_parallel_runs must be a whole number from 1 to 10"),
    ({"overlap": "parallel", "max_parallel_runs": 11}, "max_parallel_runs must be a whole number from 1 to 10"),
    ({"overlap": "parallel", "max_parallel_runs": True}, "max_parallel_runs must be a whole number from 1 to 10"),
    ({"overlap": "parallel", "max_parallel_runs": 2.5}, "max_parallel_runs must be a whole number from 1 to 10"),
    ({"run_timeout_seconds": 0}, "run_timeout_seconds must be"),
    ({"run_timeout_seconds": 1.5}, "run_timeout_seconds must be"),
    ({"run_budget_usd": -1}, "run_budget_usd must be"),
    ({"daily_budget_usd": 0}, "daily_budget_usd must be"),
    ({"gate_timeout_seconds": 601}, "gate_timeout_seconds must be"),
    ({"gate_command": "x" * 2001}, "at most 2000 characters"),
    ({"work_dir": "relative/dir"}, "absolute path"),
    ({"profile_overrides": {"manager": "agent-claude-code-opus"}}, "not 'manager'"),
    ({"task_limits": {"planner": 2}}, "task_limits: one Planner runs a task"),
    ({"task_limits": {"manager": 2}}, "task_limits: unknown role 'manager'"),
    ({"task_limits": {"worker": 0}}, "task_limits.worker: max_concurrency must be a whole number from 1 to 64"),
    ({"task_limits": {"reviewer": 65}}, "task_limits.reviewer:"),
    ({"task_limits": [8]}, "task_limits must be an object"),
    ({"enabled": True}, "created disabled"),
    ({"gate": "true"}, "unknown field 'gate'"),
])
def test_a_bad_schedule_is_refused_with_the_reason(monkeypatch: pytest.MonkeyPatch, body: dict[str, Any],
                                                   error: str) -> None:
    monkeypatch.setattr(scheduler_settings, "_min_interval", scheduler_settings.DEFAULT_MIN_INTERVAL_SECONDS)
    monkeypatch.setattr(schedules, "_existing_project", AsyncMock(return_value={"id": "p", "work_dir": "/work"}))
    full = {"name": "triage", "cron": "*/5 * * * *", "requirement": "Look at the alerts.", **body}
    with pytest.raises(RequestError) as refused:
        asyncio.run(schedules.create_schedule("p", full))
    assert refused.value.status == HTTP_400
    assert error in str(refused.value)
