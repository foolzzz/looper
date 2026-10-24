"""Workspace operations expose closed requests and generated ordinary CLI commands."""

import asyncio

import pytest

from looper import loopercli
from looper.agents import api_v1
from looper.agents.ops import OpError
from looper.agents.v1_domains.task_create import WORKSPACE_CHECK
from tests.test_api_v1_auth import _bearer, _send, _serving
from tests.test_operator_auth import TOKEN


@pytest.mark.parametrize(("command", "body", "path", "risk"), [
    (["tasks", "delete", "--task-id", "task-example"], {"task_id": "task-example"}, "tasks/delete", "high"),
    (["workspaces", "discard", "--task-id", "task-example", "--workspace-id", "9"],
     {"task_id": "task-example", "workspace_id": 9}, "workspaces/discard", "high"),
    (["workspaces", "check", "--dir", "/workspace", "--mode", "worktree", "--base", "main"],
     {"dir": "/workspace", "mode": "worktree", "base": "main"}, "workspaces/check", "read"),
])
def test_generated_cli_requests_preserve_arguments(command, body, path, risk):
    operation = api_v1.find("POST", "/api/v1/" + path)
    assert operation.risk == risk and not operation.hand and not operation.tool
    args = loopercli.build_parser(api_v1.document()).parse_args(command)
    assert (args._generated.path, args._generated.method) == (operation.path, "POST")
    assert loopercli.request_body(args, args._generated) == body
    assert operation.request["additionalProperties"] is False
    responses = api_v1.document()["paths"]["/" + path]["post"]["responses"]
    assert ("202" in responses) == (risk == "high")


@pytest.mark.parametrize(("path", "body"), [
    ("tasks/delete", {}), ("tasks/delete", {"task_id": 1}), ("tasks/delete", {"task_id": " "}),
    ("tasks/delete", {"task_id": "task-a", "force": True}),
    ("workspaces/discard", {"task_id": "task-a", "workspace_id": 0}),
    ("workspaces/discard", {"task_id": "task-a", "workspace_id": 10**18}),
    ("workspaces/discard", {"task_id": "task-a", "workspace_id": "1"}),
    ("workspaces/discard", {"task_id": "task-a", "workspace_id": True}),
    ("workspaces/check", {"dir": None}), ("workspaces/check", {"mode": "copy"}),
    ("workspaces/check", {"base": "a" * 257}), ("workspaces/check", {"extra": True}),
])
def test_invalid_requests_return_typed_errors_before_services(tmp_path, monkeypatch, path, body):
    import json
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from looper.agents.ops.clients import ClientOps, db_actions

    monkeypatch.setattr(db_actions, "record", AsyncMock())

    async def check():
        async with _serving(tmp_path) as server:
            server._client_ops = ClientOps(SimpleNamespace())
            status, _, answer = await _send(server, "POST /api/v1/" + path, _bearer(TOKEN), json.dumps(body))
            # No database is installed: validation must precede the workspace/task service.
            assert status == 400 and answer["error"]["code"] == "invalid", answer
    asyncio.run(check())


def test_check_reuses_create_schema_and_step_key_remains_optional():
    from looper.agents.step_workspaces import StepWorkspace

    assert api_v1.find("POST", "/api/v1/workspaces/check").result is WORKSPACE_CHECK
    workspace = StepWorkspace(id=1, task_id="task-a", step_id="step-a", repo_dir="/workspace", subdir="",
        target_branch="refs/heads/main", path="/workspace-step", branch="looper/task-a/step",
        base_commit="abc", checkpoint=False)
    result = {"workspace": workspace.view()}
    schema = api_v1.find("POST", "/api/v1/workspaces/discard").result
    assert api_v1.validate_arguments(schema, result) == result
    assert "step_key" not in result["workspace"]
    assert api_v1.validate_arguments(schema, {"workspace": {**result["workspace"], "step_key": "build"}})
    with pytest.raises(OpError):
        api_v1.validate_arguments(schema, {"workspace": {**result["workspace"], "state": "unknown"}})
