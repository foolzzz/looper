"""Workflow flags and generated workflow operations preserve their API payloads."""
import json

from looper import loopercli
from looper.agents import api_v1
from looper.agents.ops.clients import CLIENT_TOOLS


def test_workflow_definition_body_is_forwarded_by_generated_cli(tmp_path):
    body = {"key": "reviewed", "name": "Reviewed", "review_all": True,
            "roles": [{"key": "dev", "kind": "work", "role_prompt": "Implement the change."}]}
    path = tmp_path / "workflow.json"
    path.write_text(json.dumps(body))
    parser = loopercli.build_parser(api_v1.document())
    args = parser.parse_args(["workflows", "put", "--body", str(path)])
    assert args._generated.path == "/api/v1/workflows/put"
    assert loopercli.request_body(args, args._generated) == body
    for action in ("list", "get", "delete"):
        flags = [] if action == "list" else ["--key", "reviewed"]
        args = parser.parse_args(["workflows", action, *flags])
        assert args._generated.path == f"/api/v1/workflows/{action}"


def test_create_workflow_flag_is_available_to_client_tools_and_cli():
    tool = next(tool for tool in CLIENT_TOOLS if tool.name == "looper_task_create")
    assert tool.schema["properties"]["workflow"]["type"] == "string"
    args = loopercli.build_parser(api_v1.document()).parse_args([
        "create", "--requirement", "Build", "--project", "Example", "--workflow", "reviewed"])
    assert loopercli.request_body(args, args._generated)["workflow"] == "reviewed"
