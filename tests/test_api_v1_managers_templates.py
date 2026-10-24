"""Scenario Manager contracts and the generated standard-library CLI."""

import json
from types import SimpleNamespace

import pytest

from looper import loopercli
from looper.agents import api_v1
from looper.agents.ops import OpError
from looper.agents.ops.clients import ClientOps
from tests.test_task_agents_db import _server
from tests.test_usage_api_db import _Writer


async def _call(operation, body=None):
    """Use real v1 dispatch, preserving the service-test result/error convenience."""
    server, writer = _server(), _Writer()
    server._client_ops = ClientOps(SimpleNamespace())
    path = "/api/v1/" + operation.replace(".", "/")
    await server._handle_v1("POST", path, {}, {}, json.dumps(body or {}).encode(), writer, api_v1.OPERATOR)
    head, _, payload = writer.data.partition(b"\r\n\r\n")
    answer = json.loads(payload)
    if answer["status"] == "ok":
        api_v1.validate_arguments(api_v1.find("POST", path).result, answer["result"])
        answer = answer["result"]
    else:
        answer = {"error": answer["error"]["message"]}
    return int(head.split()[1]), answer


@pytest.mark.parametrize("family,action,options,body", [
    ("managers", "list", [], {}),
    ("managers", "create", ["--key", "review", "--name", "Review", "--system-prompt", '"Review code."'],
     {"key": "review", "name": "Review", "system_prompt": "Review code."}),
    ("managers", "update", ["--manager-id", "manager-review", "--fields", '{"enabled":false}'],
     {"manager_id": "manager-review", "fields": {"enabled": False}}),
    ("managers", "delete", ["--manager-id", "manager-review"], {"manager_id": "manager-review"}),
    ("manager_templates", "put", ["--manager-id", "manager-review", "--key", "review", "--name", "Review"],
     {"manager_id": "manager-review", "key": "review", "name": "Review"}),
    ("manager_templates", "delete", ["--manager-id", "manager-review", "--key", "review"],
     {"manager_id": "manager-review", "key": "review"}),
])
def test_generated_cli_posts_manager_operations(family, action, options, body, capsys):
    args = loopercli.build_parser(api_v1.document()).parse_args([family.replace("_", "-"), action, *options])
    calls = []
    api = SimpleNamespace(call=lambda *parts: (calls.append(parts) or (200, {"status": "ok", "result": {}})))
    assert args._run(args, SimpleNamespace(api=lambda: api, as_json=True, session="test")) == 0
    assert calls == [("POST", f"/api/v1/{family}/{action}", body)]


@pytest.mark.parametrize("operation,body", [
    ("managers.list", {"unknown": True}),
    ("managers.create", {"key": "review", "name": "Review"}),
    ("managers.create", {"key": "review", "name": "Review", "system_prompt": "x", "enabled": "yes"}),
    ("managers.update", {"manager_id": "manager-review", "fields": {}}),
    ("managers.update", {"manager_id": "manager-review", "fields": {"key": "other"}}),
    ("managers.delete", {"manager_id": 1}),
    ("manager_templates.put", {"manager_id": "manager-review", "key": "pr"}),
    ("manager_templates.put", {"manager_id": "manager-review", "key": "pr", "name": "Review", "project_id": 1}),
    ("manager_templates.put", {"manager_id": "manager-review", "key": "pr", "name": "Review", "profiles": {"manager": "p"}}),
    ("manager_templates.delete", {"manager_id": "manager-review", "key": None}),
])
def test_requests_distinguish_optional_nullable_and_closed_fields(operation, body):
    op = next(op for op in api_v1.operations() if op.operation_id == operation)
    assert not op.hand
    with pytest.raises(OpError):
        api_v1.validate_arguments(op.request, body)
