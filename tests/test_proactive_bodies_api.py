"""Only the operator can read retained proactive text, including through the CLI."""
import asyncio
import json
from unittest.mock import AsyncMock

import pytest

from looper import loopercli
from looper.agents import api_v1
from looper.db import proactive_deliveries
from tests.test_api_v1_auth import CLIENT_TOKEN, _bearer, _send, _serving, clients
from tests.test_operator_auth import TOKEN
from tests.test_loopercli import Server, _send as cli_send


def test_operator_positive_control_and_client_denied_before_read(tmp_path, monkeypatch, clients):
    read = AsyncMock(return_value={"deliveries": [], "next_before": None})
    monkeypatch.setattr(proactive_deliveries, "recent_bodies", read)

    async def check():
        async with _serving(tmp_path) as server:
            path = "POST /api/v1/proactive_sends/list"
            status, _, answer = await _send(server, path, _bearer(TOKEN), '{}')
            assert status == 200 and answer["result"] == {"deliveries": [], "next_before": None}
            read.assert_awaited_once()
            read.reset_mock()
            for body in ('{}', '{"limit": 0}', '{"source": "bad"}'):
                status, _, answer = await _send(server, path, _bearer(CLIENT_TOKEN), body)
                assert status == 403 and answer["error"]["code"] == "forbidden"
            read.assert_not_awaited()
    asyncio.run(check())


@pytest.mark.parametrize("arguments", [{"limit": 0}, {"limit": 101}, {"before": -1},
    {"source": "invalid"}, {"local_date": "2026-02-30"}, {"extra": True}])
def test_invalid_filters_do_not_reach_storage(tmp_path, monkeypatch, arguments):
    read = AsyncMock()
    monkeypatch.setattr(proactive_deliveries, "recent_bodies", read)

    async def check():
        async with _serving(tmp_path) as server:
            status, _, _ = await _send(server, "POST /api/v1/proactive_sends/list", _bearer(TOKEN), json.dumps(arguments))
            assert status == 400
            read.assert_not_awaited()
    asyncio.run(check())


def test_operation_is_generated_for_the_cli(tmp_path):
    def respond(handler, request):
        cli_send(handler, 200, api_v1.document() if request[1].endswith("openapi.json")
                 else {"status": "ok", "result": {"deliveries": [], "next_before": None}})
    server = Server(respond)
    try:
        token = tmp_path / "operator-token"
        token.write_text(TOKEN + "\n")
        token.chmod(0o600)
        assert loopercli.main(["--client-file", str(tmp_path / "missing-client.json"), "--json", "proactive-sends",
            "list", "--source", "morning_brief", "--limit", "2", "--url", server.url,
            "--operator-token-file", str(token)]) == 0
        assert server.requests[-1][1] == "/api/v1/proactive_sends/list"
        assert server.requests[-1][3] == {"source": "morning_brief", "limit": 2}
    finally:
        server.close()
    operation = api_v1.find("POST", "/api/v1/proactive_sends/list")
    assert operation.operator_only and operation.cli and operation.tool is None
