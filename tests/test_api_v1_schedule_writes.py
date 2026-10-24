"""Schedule write schemas and OpenAPI-generated standard-library CLI requests."""

from types import SimpleNamespace

import pytest

from looper import loopercli
from looper.agents import api_v1
from looper.agents.ops import OpError


@pytest.mark.parametrize("action,options,body", [
    ("create", ["--project-id", "project-a", "--fields", '{"name":"Hourly","cron":"0 * * * *","requirement":"Read reports"}'],
     {"project_id": "project-a", "fields": {"name": "Hourly", "cron": "0 * * * *", "requirement": "Read reports"}}),
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
    ("update", {"schedule_id": "sched-a", "fields": {}}),
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
