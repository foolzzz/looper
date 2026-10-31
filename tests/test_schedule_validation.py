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
    ({"user_gates": "yes"}, "user_gates must be one of off, on"),
    ({"user_gates": None}, "user_gates must be one of off, on"),
    ({"user_gates": True}, "user_gates must be one of off, on"),
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


@pytest.mark.parametrize(("body", "error"), [
    ({}, "command is required"),
    ({"command": "  "}, "command is required"),
    ({"command": "x" * 2001}, "command"),
    ({"command": "echo\x00x"}, "command"),
    ({"command": 1}, "command"),
    *[({"command": "true", field: value}, field) for field, value in (
        ("requirement", "Read reports"), ("template_key", "report"), ("template_inputs", {"x": "y"}),
        ("gate_command", "true"), ("gate_timeout_seconds", 5), ("run_budget_usd", 1),
        ("daily_budget_usd", 1), ("profile_overrides", {"worker": "missing-profile"}),
        ("task_limits", {"worker": 1}), ("user_gates", "on"),
    )],
    ({"command": "true", "run_timeout_seconds": "default"}, "run_timeout_mode"),
    *[({"command": "true", "env": value}, "env") for value in (
        [], {"1A": "x"}, {"LOOPER_SCHEDULE_X": "x"}, {"A": "x" * 4097},
        {"A": "x\x00y"}, {"A": 1}, {f"A{i}": "x" for i in range(65)},
    )],
    *[({"command": "true", "success_deadline": value}, "success_deadline") for value in (
        "8:00", "24:00", "12:60", "12:00\n", 123,
    )],
    *[({"command": "true", field: value}, field) for field, low, high in (
        ("alert_failures_after", 1, 1000), ("alert_stale_after_seconds", 300, 2592000),
        ("alert_overlap_after", 0, 10000),
    ) for value in (low - 1, high + 1, True, 1.5)],
])
def test_command_fields_are_validated_before_writing(monkeypatch, body, error):
    monkeypatch.setattr(schedules, "_existing_project", AsyncMock(return_value={"id": "p"}))
    with pytest.raises(RequestError) as refused:
        asyncio.run(schedules.create_schedule("p", {"name": "Command", "cron": "* * * * *",
                                                   "timezone": "UTC", "kind": "command", **body}))
    assert refused.value.status == HTTP_400
    assert error in str(refused.value)


@pytest.mark.parametrize(("field", "value"), [("command", "true"), ("env", {"A": "B"}),
                                             ("env_file", "file")])
def test_agent_refuses_command_only_fields(monkeypatch, tmp_path, field, value):
    if field == "env_file":
        path = tmp_path / "env"
        path.write_text("A=B")
        value = str(path)
    monkeypatch.setattr(schedules, "_existing_project", AsyncMock(return_value={"id": "p"}))
    with pytest.raises(RequestError) as refused:
        asyncio.run(schedules.create_schedule("p", {"name": "Agent", "cron": "*/5 * * * *",
            "requirement": "Read", "timezone": "UTC", field: value}))
    assert refused.value.status == HTTP_400
    assert f"{field} applies to command schedules only" in str(refused.value)


@pytest.mark.parametrize("value", ["relative/path", "missing", "directory", "unreadable", "x" * 1025])
def test_env_file_requires_a_readable_absolute_file(tmp_path, monkeypatch, value):
    if value in ("missing", "directory", "unreadable"):
        path = tmp_path / value
        if value == "directory":
            path.mkdir()
        elif value == "unreadable":
            path.touch()
            monkeypatch.setattr(schedules.os, "access", lambda *args: False)
        value = str(path)
    with pytest.raises(ValueError, match="env_file"):
        schedules._env_file(value)


def test_env_file_preserves_spelling_and_import_can_defer_existence(tmp_path, monkeypatch):
    monkeypatch.setattr(schedules.os.path, "expanduser", lambda path: str(tmp_path / "env"))
    (tmp_path / "env").write_text("A=B")
    assert schedules._env_file("~/env") == "~/env"
    assert schedules._env_file(str(tmp_path / "missing"), must_exist=False)
    assert schedules._env_file("") is None
    assert schedules._env_file(None) is None


def test_kind_is_immutable_at_the_service_boundary():
    with pytest.raises(RequestError, match="a schedule's kind cannot change") as refused:
        asyncio.run(schedules._fields({"kind": "command"}, update=True))
    assert refused.value.status == HTTP_400


@pytest.mark.parametrize("kind", ["agent", "command"])
def test_one_minute_rules_are_only_allowed_for_commands_or_gates(monkeypatch, kind):
    from datetime import datetime, timezone
    monkeypatch.setattr(scheduler_settings, "_min_interval", scheduler_settings.DEFAULT_MIN_INTERVAL_SECONDS)
    args = ("* * * * *", "UTC", datetime(2026, 10, 31, tzinfo=timezone.utc))
    if kind == "command":
        assert schedules._check_rule(*args, gated=True)
    else:
        with pytest.raises(RequestError, match="at least 5 minutes"):
            schedules._check_rule(*args, gated=False)


@pytest.mark.parametrize(("kind", "overrides", "expected"), [
    ("command", {}, (3, None, 10)), ("agent", {}, (3, None, 0)),
    ("command", {"alert_failures_after": 1, "alert_stale_after_seconds": 300,
                 "alert_overlap_after": 0}, (1, 300, 0)),
])
def test_alert_policy_resolves_defaults_and_explicit_zero(kind, overrides, expected):
    from looper.agents.schedule_notify import alert_policy
    policy = alert_policy({"kind": kind, **overrides})
    assert (policy.failures_after, policy.stale_after.total_seconds() if policy.stale_after else None,
            policy.overlap_after) == expected


@pytest.mark.parametrize(("status", "error"), [("running", "not been written yet"),
                                              ("succeeded", "retention policy")])
def test_run_log_handles_removal_before_the_thread_opens_it(tmp_path, monkeypatch, status, error):
    path = tmp_path / "logs" / "schedules" / "run.log"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"output")
    monkeypatch.setattr(schedules.db_schedules, "get_run", AsyncMock(return_value={
        "schedule_id": "schedule", "log_path": "logs/schedules/run.log", "status": status}))
    to_thread = asyncio.to_thread

    async def removed_before_read(read):
        path.unlink()
        return await to_thread(read)

    monkeypatch.setattr(schedules.asyncio, "to_thread", removed_before_read)
    with pytest.raises(RequestError, match=error) as refused:
        asyncio.run(schedules.run_log("run", {}, tmp_path))
    assert refused.value.status == schedules.HTTP_404


def test_run_log_reports_retention_removal_during_read(tmp_path, monkeypatch):
    from contextlib import contextmanager
    from pathlib import Path

    path = tmp_path / "logs" / "schedules" / "run.log"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"output")
    monkeypatch.setattr(schedules.db_schedules, "get_run", AsyncMock(return_value={
        "schedule_id": "schedule", "log_path": "logs/schedules/run.log", "status": "succeeded"}))
    original_open = Path.open

    @contextmanager
    def open_then_remove(target, *args, **kwargs):
        with original_open(target, *args, **kwargs) as stream:
            class RemovedWhileReading:
                fileno = stream.fileno
                seek = stream.seek

                def read(self, size):
                    data = stream.read(size)
                    target.unlink()
                    return data

            yield RemovedWhileReading()

    monkeypatch.setattr(Path, "open", open_then_remove)
    with pytest.raises(RequestError, match="retention policy") as refused:
        asyncio.run(schedules.run_log("run", {}, tmp_path))
    assert refused.value.status == schedules.HTTP_404
