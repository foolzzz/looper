"""The full task-create projection and its generated CLI request."""

import pytest

from looper import loopercli
from looper.agents import api_v1
from looper.agents.v1_domains.task_create import NEW_PROJECT_FIELDS, WORKSPACE_CHECK
from looper.agents.project_execution import FIELDS

PATH = "/api/v1/tasks/create"


def test_create_declares_the_full_closed_request_and_workspace_result():
    operation = api_v1.find("POST", PATH)
    assert operation.operation_id == "tasks.create" and not operation.hand and not operation.tool
    assert set(FIELDS) <= NEW_PROJECT_FIELDS["properties"].keys()
    assert operation.result["properties"]["workspace"] == WORKSPACE_CHECK
    workspace = {"workspace": "/workspace", "mode": "worktree", "requested_mode": "worktree",
                 "default_mode": "worktree", "git": {"repository": "/workspace", "branch": None,
                 "head": "abc", "base_commit": "abc"}, "warnings": [{"kind": "large_checkout", "text": "Large",
                 "bytes": 123, "files": ["source.py"], "tasks": [{"task_id": "task-a", "name": "Build",
                 "status": "running", "mode": "in_place"}]}], "notes": {"found": True,
                 "files": ["CLAUDE.md"], "omitted": [], "error": "unreadable"}, "notes_on": False}
    api_v1.validate_arguments(operation.result, {"task_id": "task-a", "status": "init",
        "created_at": "2026-10-23T00:00:00Z", "project_id": "proj-a", "workspace": workspace})
    document = api_v1.document()["paths"]["/tasks/create"]["post"]
    assert "202" in document["responses"] and "x-looper-cli-hand" not in document


@pytest.mark.parametrize("choice", [{"project_id": "proj-a"}, {"project": {"name": "Example"}}])
def test_generated_cli_preserves_the_full_json_request(choice):
    import json

    payload = {"requirement": "Build", **choice, "config": {"nested": [False, 3]},
               "profile_overrides": {"worker": "profile-a"}, "workspace_mode": "worktree", "base": "main"}
    flags = ["tasks", "create"]
    for key, value in payload.items():
        flags += ["--" + key.replace("_", "-"), value if isinstance(value, str) else json.dumps(value)]
    args = loopercli.build_parser(api_v1.document()).parse_args(flags)
    assert args._generated.path == PATH
    assert loopercli.request_body(args, args._generated) == payload
