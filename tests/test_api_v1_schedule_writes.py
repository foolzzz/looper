"""Schedule write schemas and OpenAPI-generated standard-library CLI requests."""

import asyncio
from types import SimpleNamespace

import pytest

from looper import loopercli
from looper.agents import api_v1
from looper.agents.ops import OpError
from looper.agents.v1_domains import schedule_writes


@pytest.mark.parametrize("action,options,body", [
    ("create", ["--project-id", "project-a", "--fields", '{"name":"Hourly","cron":"0 * * * *","requirement":"Read reports"}'],
     {"project_id": "project-a", "fields": {"name": "Hourly", "cron": "0 * * * *", "requirement": "Read reports"}}),
    ("create", ["--project-id", "project-a", "--fields", '{"name":"Hourly","cron":"0 * * * *","requirement":"Read reports","manager_id":"manager-reports"}'],
     {"project_id": "project-a", "fields": {"name": "Hourly", "cron": "0 * * * *", "requirement": "Read reports", "manager_id": "manager-reports"}}),
    ("update", ["--schedule-id", "sched-a", "--fields", '{"enabled":true}'],
     {"schedule_id": "sched-a", "fields": {"enabled": True}}),
    *[(action, ["--schedule-id", "sched-a"], {"schedule_id": "sched-a"})
      for action in ("delete", "run", "reset_planner_session")],
])
def test_generated_cli_posts_declared_schedule_write(action, options, body):
    args = loopercli.build_parser(api_v1.document()).parse_args(["schedule-admin", action.replace("_", "-"), *options])
    calls = []
    api = SimpleNamespace(call=lambda *parts: (calls.append(parts) or (200, {"status": "ok", "result": {}})))
    ctx = SimpleNamespace(api=lambda: api, as_json=True, session="schedule-test")
    assert args._run(args, ctx) == 0
    assert calls == [("POST", f"/api/v1/schedule_admin/{action}", body)]
    assert api_v1.find("POST", calls[0][1]).hand is False


@pytest.mark.parametrize("action,body", [
    ("create", {"project_id": 1, "fields": {}}),
    ("create", {"project_id": "project-a", "fields": {"name": "Hourly", "cron": "0 * * * *", "requirement": "Read", "enabled": False}}),
    *[("create", {"project_id": "project-a", "fields": {"name": "Hourly", "cron": "0 * * * *", "requirement": "Read", "manager_id": owner}})
      for owner in ("", None, 1)],
    ("update", {"schedule_id": "sched-a", "fields": {}}),
    ("update", {"schedule_id": "sched-a", "fields": {"manager_id": "manager-reports"}}),
    ("update", {"schedule_id": "sched-a", "fields": {"kind": "shell"}}),
    ("update", {"schedule_id": "sched-a", "fields": {"max_parallel_runs": True}}),
    ("update", {"schedule_id": "sched-a", "fields": {"profile_overrides": {"manager": "profile-a"}}}),
    ("update", {"schedule_id": "sched-a", "fields": {"task_limits": {"planner": 1}}}),
    *[("update", {"schedule_id": "sched-a", "fields": {key: value}})
      for key, value in (("run_budget_usd", "2.5"), ("daily_budget_usd", "10"),
                         ("run_timeout_seconds", "60"), ("run_timeout_seconds", 0),
                         ("max_parallel_runs", 11), ("gate_timeout_seconds", 0),
                         ("task_limits", {"worker": "2"}))],
    ("run", {"schedule_id": 1}), ("delete", {"schedule_id": "sched-a", "extra": True}),
    ("reset_planner_session", {}),
])
def test_schedule_write_requests_are_closed_and_require_typed_fields(action, body):
    op = api_v1.find("POST", f"/api/v1/schedule_admin/{action}")
    with pytest.raises(OpError):
        api_v1.validate_arguments(op.request, body)


@pytest.mark.parametrize("fields", [
    {"kind": "command", "command": "printf ready"},
    {"kind": "command", "command": "printf ready", "env": None},
    {"kind": "agent", "requirement": "Read reports"},
    {"kind": "command", "command": "printf ready", "env": {"MODE": "daily"}, "env_file": None,
     "success_deadline": "08:00", "alert_failures_after": 1, "alert_stale_after_seconds": 300,
     "alert_overlap_after": 0, "run_timeout_seconds": "default"},
])
def test_schedule_create_accepts_command_fields_without_requirement(fields):
    api_v1.validate_arguments(schedule_writes.SCHEDULE_CREATE, {"name": "Hourly", "cron": "* * * * *", **fields})


@pytest.mark.parametrize("field,value", [
    ("kind", "shell"), ("command", 3), ("env", {"MODE": 3}), ("env_file", 3),
    ("success_deadline", "8:00"), ("success_deadline", "24:00"),
    ("alert_failures_after", 0), ("alert_failures_after", 1001),
    ("alert_stale_after_seconds", 299), ("alert_stale_after_seconds", 2592001),
    ("alert_overlap_after", -1), ("alert_overlap_after", 10001), ("alert_overlap_after", True),
])
def test_schedule_command_field_schemas_reject_invalid_values(field, value):
    with pytest.raises(OpError):
        api_v1.validate_arguments(schedule_writes.SCHEDULE_CREATE,
                                  {"name": "Hourly", "cron": "* * * * *", field: value})


def test_schedule_patch_accepts_command_and_alert_fields():
    api_v1.validate_arguments(schedule_writes.SCHEDULE_PATCH,
                              {"command": "printf ready", "env": {}, "env_file": None, "success_deadline": None,
                               "alert_failures_after": None, "alert_stale_after_seconds": None,
                               "alert_overlap_after": None})


@pytest.mark.parametrize("kind", ["command", "agent"])
def test_update_impact_checks_minute_cron_for_schedule_kind(monkeypatch, kind):
    async def existing(schedule_id):
        return {"name": "Hourly", "project_name": "Reports", "project_id": "proj-example", "enabled": False,
                "kind": kind, "command": "true" if kind == "command" else None,
                "gate_command": None, "cron": "0 * * * *", "timezone": "UTC"}

    async def project(project_id):
        return None

    monkeypatch.setattr(schedule_writes.schedules, "_existing", existing)
    monkeypatch.setattr(schedule_writes.db_projects, "get_project", project)
    operation = api_v1.find("POST", "/api/v1/schedule_admin/update")
    request = schedule_writes.impact(None, operation,
                                     {"schedule_id": "sched-example", "fields": {"cron": "* * * * *"}}, None, {})
    if kind == "agent":
        with pytest.raises(OpError):
            asyncio.run(request)
    else:
        assert asyncio.run(request) is None
