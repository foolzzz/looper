"""Task configuration's declared v1 contracts and generated, standard-library CLI requests."""

from types import SimpleNamespace

import pytest

from looper import loopercli
from looper.agents import api_v1
from looper.agents.agent_settings import CONCURRENCY_MAX
from looper.agents.ops import OpError


@pytest.mark.parametrize("action,options,body", [
    ("configure", ["--requirement", "Build calculator", "--config", '{"verify":true}'],
     {"requirement": "Build calculator", "config": {"verify": True}}),
    ("move", ["--project-id", "project-new"], {"project_id": "project-new"}),
    ("agents", ["--concurrency", '{"worker":2}', "--system-prompt-modes", '{"worker":null}'],
     {"concurrency": {"worker": 2}, "system_prompt_modes": {"worker": None}}),
    ("continue", ["--task-budget-usd", "2.5"], {"task_budget_usd": 2.5}),
])
def test_generated_cli_posts_the_declared_task_operation(action, options, body, capsys):
    args = loopercli.build_parser(api_v1.document()).parse_args(["tasks", action, "--task-id", "task-a", *options])
    calls = []
    api = SimpleNamespace(call=lambda *parts: (calls.append(parts) or (200, {"status": "ok", "result": {}})))
    ctx = SimpleNamespace(api=lambda: api, as_json=True, session="test")
    assert args._run(args, ctx) == 0
    assert calls == [("POST", f"/api/v1/tasks/{action}", {"task_id": "task-a", **body})]


@pytest.mark.parametrize("value", [0, -1, CONCURRENCY_MAX + 1, 1.5, True, "2"])
def test_agents_schema_enforces_existing_numeric_limits(value):
    op = next(op for op in api_v1.operations() if op.operation_id == "tasks.agents")
    with pytest.raises(OpError):
        api_v1.validate_arguments(op.request, {"task_id": "task-a", "concurrency": {"worker": value}})


@pytest.mark.parametrize("action,body", [
    ("configure", {"requirement": None}), ("configure", {"work_dir": None}),
    ("configure", {"config": None}), ("move", {"project_id": 1}),
    ("agents", {"profiles": {"manager": "profile-a"}}),
    ("agents", {"system_prompt_modes": {"worker": "other"}}),
    ("continue", {"task_budget_usd": None}),
])
def test_optional_fields_are_not_nullable_and_requests_are_closed(action, body):
    op = next(op for op in api_v1.operations() if op.operation_id == f"tasks.{action}")
    with pytest.raises(OpError):
        api_v1.validate_arguments(op.request, {"task_id": "task-a", **body})
    with pytest.raises(OpError):
        api_v1.validate_arguments(op.request, {"task_id": "task-a", **body, "unknown": True})
