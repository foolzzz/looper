"""Schedule preview validation and generated CLI requests use ordinary read operations."""

import asyncio
import json

import pytest

from looper import loopercli
from looper.agents import api_v1
from looper.agents.v1_domains import schedule_reads
from tests.test_api_v1_auth import _bearer, _send, _serving
from tests.test_loopercli import Server, _send as cli_send, write_client_file
from tests.test_operator_auth import TOKEN


@pytest.mark.parametrize("cron,gated,valid", [("*/5 * * * *", False, True), ("* * * * *", True, True),
                                             ("not a cron", False, False), ("* * * * *", False, False)])
def test_preview_preserves_invalid_rules_as_normal_results(tmp_path, cron, gated, valid):
    async def run():
        async with _serving(tmp_path) as server:
            status, _, answer = await _send(server, "POST /api/v1/schedule_admin/preview", _bearer(TOKEN),
                                            json.dumps({"cron": cron, "gated": gated, "count": 2, "timezone": "UTC"}))
            assert status == 200 and answer["status"] == "ok"
            result = answer["result"]
            api_v1.validate_arguments(schedule_reads.PREVIEW, result)
            assert result["valid"] is valid
            assert len(result["next"]) == (2 if valid else 0)
            assert bool(result["error"]) is not valid
    asyncio.run(run())


@pytest.mark.parametrize("body", [{}, {"cron": 3}, {"cron": "0 * * * *", "count": True},
    {"cron": "0 * * * *", "count": 0}, {"cron": "0 * * * *", "count": 21},
    {"cron": "0 * * * *", "timezone": None}, {"cron": "0 * * * *", "gated": "yes"},
    {"cron": "0 * * * *", "extra": 1}])
def test_malformed_preview_is_a_typed_invalid_request(tmp_path, body):
    async def run():
        async with _serving(tmp_path) as server:
            status, _, answer = await _send(server, "POST /api/v1/schedule_admin/preview", _bearer(TOKEN), json.dumps(body))
            assert status == 400 and answer["error"]["code"] == "invalid"
    asyncio.run(run())


@pytest.mark.parametrize("action,args,body", [
    ("list", [], {}), ("project_list", ["--project-id", "proj-example"], {"project_id": "proj-example"}),
    ("get", ["--schedule-id", "sched-example"], {"schedule_id": "sched-example"}),
    ("preview", ["--cron", "0 * * * *", "--count", "20", "--gated"], {"cron": "0 * * * *", "count": 20, "gated": True}),
    ("runs", ["--schedule-id", "sched-example", "--limit", "200", "--skips", "hide"],
     {"schedule_id": "sched-example", "limit": 200, "skips": "hide"}),
    ("notifications", ["--schedule-id", "sched-example", "--limit", "1"], {"schedule_id": "sched-example", "limit": 1})])
def test_generated_schedule_commands_send_the_declared_request(tmp_path, capsys, action, args, body):
    def respond(handler, request):
        cli_send(handler, 200, api_v1.document() if request[1].endswith("openapi.json")
                 else {"status": "ok", "result": {"received": True}})
    server = Server(respond)
    try:
        client = tmp_path / "client.json"
        write_client_file(client, server.url)
        assert loopercli.main(["--client-file", str(client), "--json", "schedule-admin", action.replace("_", "-"), *args]) == 0
        method, path, _, sent = server.requests[-1]
        assert (method, path, sent) == ("POST", f"/api/v1/schedule_admin/{action}", body)
        op = api_v1.find(method, path)
        assert not op.hand and op.risk == "read" and op.tool is None
        assert "202" not in api_v1.document()["paths"][path.removeprefix("/api/v1")]["post"]["responses"]
    finally:
        server.close()
