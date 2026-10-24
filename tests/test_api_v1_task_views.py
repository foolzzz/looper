"""Task view input failures and generated, standard-library CLI requests."""

import asyncio
import json

import pytest

from looper import loopercli
from looper.agents import api_v1
from tests.test_api_v1_auth import _bearer, _send, _serving
from tests.test_loopercli import Server, _send as cli_send, write_client_file
from tests.test_operator_auth import TOKEN


@pytest.mark.parametrize("action,body", [
    ("page", {"group": "unknown"}), ("page", {"status": "unknown"}),
    ("page", {"limit": 0}), ("page", {"limit": 201}), ("page", {"limit": True}),
    ("page", {"before": 1}), ("page", {"query": None}), ("page", {"project_id": 1}),
    ("page", {"schedule_id": None}), ("page", {"extra": 1}),
    ("detail", {}), ("detail", {"task_id": ""}), ("detail", {"task_id": 1}),
])
def test_invalid_filters_are_public_typed_failures(tmp_path, action, body):
    async def run():
        async with _serving(tmp_path) as server:
            status, _, answer = await _send(server, f"POST /api/v1/tasks/{action}",
                                            _bearer(TOKEN), json.dumps(body))
            assert status == 400 and answer["error"]["code"] == "invalid"
    asyncio.run(run())


@pytest.mark.parametrize("action,args,body", [
    ("page", ["--project-id", "proj-example", "--schedule-id", "schedule-example", "--group", "ready",
              "--status", "init", "--before", "task-example", "--limit", "2", "--query", "calculator"],
     {"project_id": "proj-example", "schedule_id": "schedule-example", "group": "ready", "status": "init",
      "before": "task-example", "limit": 2, "query": "calculator"}),
    ("detail", ["--task-id", "task-example"], {"task_id": "task-example"}),
])
def test_generated_cli_sends_ordinary_read_operation(tmp_path, capsys, action, args, body):
    def respond(handler, request):
        cli_send(handler, 200, api_v1.document() if request[1].endswith("openapi.json")
                 else {"status": "ok", "result": {"received": True}})
    server = Server(respond)
    try:
        client = tmp_path / "client.json"
        write_client_file(client, server.url)
        assert loopercli.main(["--client-file", str(client), "--json", "tasks", action, *args]) == 0
        method, path, _, sent = server.requests[-1]
        assert (method, path, sent) == ("POST", f"/api/v1/tasks/{action}", body)
        op = api_v1.find(method, path)
        assert op.risk == "read" and not op.hand and not op.operator_only and op.tool is None
    finally:
        server.close()
