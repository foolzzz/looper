"""Workspace operation contracts and their generated standard-library CLI requests."""

import pytest

from looper import loopercli
from looper.agents import api_v1
from looper.agents.ops import OpError
from looper.agents.step_workspaces import StepWorkspace
from looper.agents.v1_domains.task_create import WORKSPACE_CHECK
from looper.agents.v1_domains.workspaces import STEP_WORKSPACE


@pytest.mark.parametrize("operation_id,payload,flags", [
    ("tasks.delete", {"task_id": "task-example"}, ["--task-id", "task-example"]),
    ("workspaces.discard", {"task_id": "task-example", "workspace_id": 19},
     ["--task-id", "task-example", "--workspace-id", "19"]),
    ("workspaces.check", {"dir": "/workspace", "mode": "worktree", "base": "main"},
     ["--dir", "/workspace", "--mode", "worktree", "--base", "main"]),
])
def test_generated_cli_uses_the_closed_ordinary_operation(operation_id, payload, flags):
    path = "/api/v1/" + operation_id.replace(".", "/")
    operation = api_v1.find("POST", path)
    assert operation.operation_id == operation_id and not operation.hand and not operation.tool
    assert operation.request["additionalProperties"] is False
    assert operation.risk == ("read" if operation_id == "workspaces.check" else "high")
    args = loopercli.build_parser(api_v1.document()).parse_args([*operation_id.split("."), *flags])
    assert args._generated.path == path
    assert loopercli.request_body(args, args._generated) == payload
    api_v1.validate_arguments(operation.request, payload)
    responses = api_v1.document()["paths"][path.removeprefix("/api/v1")]["post"]["responses"]
    assert ("202" in responses) == (operation.risk == "high")


@pytest.mark.parametrize("workspace_id", [0, 10**18, "1", 1.5, True, None])
def test_discard_rejects_ids_outside_the_integer_contract(workspace_id):
    operation = api_v1.find("POST", "/api/v1/workspaces/discard")
    with pytest.raises(OpError):
        api_v1.validate_arguments(operation.request, {"task_id": "task-example", "workspace_id": workspace_id})


def test_workspace_projection_accepts_older_rows_without_optional_step_label():
    row = {"id": 1, "task_id": "task-example", "step_id": "step-one", "repo_dir": "/workspace",
           "target_branch": "refs/heads/main", "path": "/workspace/step", "branch": "step-branch",
           "base_commit": "abc", "state": "active"}
    view = StepWorkspace.from_row(row).view()
    api_v1.validate_arguments(STEP_WORKSPACE, view)
    assert view["links"] == view["merge_files"] == [] and view["merge_commit"] is None
    operation = api_v1.find("POST", "/api/v1/workspaces/discard")
    api_v1.validate_arguments(operation.result, {"workspace": {**view, "step_key": "build"}})
    assert api_v1.find("POST", "/api/v1/workspaces/check").result == WORKSPACE_CHECK
