"""The existing costs Tool is typed and discovered by the generated CLI."""

import json

import pytest

from looper import loopercli
from looper.agents import api_v1
from looper.agents.ops.clients import CLIENT_TOOLS
from tests.test_loopercli import Server, _send, write_client_file


def test_costs_keeps_its_tool_request_and_read_transport():
    operation = api_v1.find("POST", "/api/v1/ops/costs")
    tool = next(tool for tool in CLIENT_TOOLS if tool.name == "looper_costs")
    assert operation.request == tool.schema
    assert operation.tool == tool.name and operation.risk == "read"
    assert not operation.hand and not operation.confirms
    assert len(operation.result["oneOf"]) == 3
    response = api_v1.document()["paths"]["/ops/costs"]["post"]["responses"]
    assert "202" not in response
    assert response["200"]["content"]["application/json"]["schema"]["allOf"][1]["properties"]["result"] == operation.result


@pytest.mark.parametrize("args,body", [
    ([], {}),
    (["--task-id", "task-example"], {"task_id": "task-example"}),
    (["--project", "Example", "--period", "this_month"], {"project": "Example", "period": "this_month"}),
    (["--since", "2026-01-01", "--until", "2026-01-31"], {"since": "2026-01-01", "until": "2026-01-31"}),
])
def test_generated_costs_parser_preserves_requests_and_nonempty_results(tmp_path, capsys, args, body):
    result = {"since": None, "until": None,
              "total": {"provider_priced_usd": 0, "equivalent_usd": 0.5, "runs": 2, "running": 0,
                        "unpriced_runs": 1},
              "projects": [{"project": None, "provider_priced_usd": 0, "equivalent_usd": 0.5,
                            "runs": 2, "tasks": 1}], "note_projects": "Includes task runs", "note": "Spend basis"}

    def respond(handler, request):
        _send(handler, 200, api_v1.document() if request[1].endswith("openapi.json")
              else {"status": "ok", "result": result})

    server = Server(respond)
    try:
        client = tmp_path / "client.json"
        write_client_file(client, server.url)
        assert loopercli.main(["--client-file", str(client), "--json", "costs", *args]) == 0
        method, path, _, sent = server.requests[-1]
        assert (method, path, sent) == ("POST", "/api/v1/ops/costs", body)
        assert json.loads(capsys.readouterr().out) == {"status": "ok", "result": result}
    finally:
        server.close()
