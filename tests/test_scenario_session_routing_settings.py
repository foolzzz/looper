"""Scenario routing is an operator setting, persisted on the Manager definition."""

import asyncio
import json
from types import SimpleNamespace

import pytest

from looper.agents import api_v1, managers_api
from looper.agents.action_levels import ActionLevel
from looper.agents.ops import Turn
from looper.agents.ops.clients import ClientOps
from looper.agents.ops.service import MANAGER_TOOLS, ManagerOps
from tests.db_support import run_db
from tests.test_api_v1_auth import CLIENT_TOKEN, _bearer, _send, _serving, clients  # noqa: F401
from tests.test_operator_auth import TOKEN


@pytest.mark.parametrize("action", ["get", "update"])
@pytest.mark.usefixtures("clients")
def test_scenario_routing_operations_refuse_client_and_group_tools(tmp_path, monkeypatch, action):
    operation = api_v1.find("POST", f"/api/v1/scenario_session_routing/{action}")
    assert operation is not None and operation.tool is None
    assert operation.level is ActionLevel.INSIDE
    assert operation.risk == ("high" if action == "update" else "read")
    assert operation.confirms is (action == "update")
    assert not any(tool.name == "looper_scenario_session_routing" for tool in MANAGER_TOOLS)

    async def check(pool):
        await managers_api.create_manager({"key": "routing", "name": "Routing", "system_prompt": "Handle this chat."})
        body = {"manager_id": "manager-routing"}
        if action == "update":
            body["session_routing"] = "per_chat"
        async with _serving(tmp_path) as server:
            server._client_ops = ClientOps(SimpleNamespace())
            status, _, answer = await _send(server, f"POST {operation.path}", _bearer(CLIENT_TOKEN), json.dumps(body))
            assert status == 403 and answer["error"]["code"] == "forbidden", answer
        row = await pool.fetchrow("SELECT session_routing FROM managers WHERE id='manager-routing'")
        assert row["session_routing"] == "off"
        ops = ManagerOps(SimpleNamespace(), url="http://127.0.0.1:1", turn_file=tmp_path / "turn.json")
        ops.begin_turn(Turn(channel="lark", channel_id="channel-synthetic", chat="oc_synthetic", group=True))
        for tool, arguments in (("looper_scenario_session_routing", body),
                                ("looper_settings", {"action": "scenario_session_routing", **body})):
            refused = await ops.call(tool, arguments)
            assert refused["status"] == "error" and refused["error"]["code"] == "invalid", refused
        assert await pool.fetchval("SELECT session_routing FROM managers WHERE id='manager-routing'") == "off"
        with pytest.raises(managers_api.RequestError, match="unknown field"):
            await managers_api.update_manager("manager-routing", {"session_routing": "per_chat"})

    run_db(check, monkeypatch)
    assert operation.operator_only


def test_operator_persistence_readback_null_and_audit(tmp_path, monkeypatch):
    async def check(pool):
        await managers_api.create_manager({"key": "routing", "name": "Routing", "system_prompt": "Handle this chat."})
        row = await pool.fetchrow("SELECT * FROM managers WHERE id='manager-routing'")
        assert managers_api.ManagerDefinition.from_row(row).session_routing.value == "off"
        async with _serving(tmp_path) as server:
            server._client_ops = ClientOps(SimpleNamespace())
            for action, value, expected in (("get", None, "off"), ("update", "per_chat", "per_chat"),
                                             ("get", None, "per_chat"), ("update", None, "off")):
                body = {"manager_id": "manager-routing"}
                if action == "update":
                    body["session_routing"] = value
                status, _, answer = await _send(server, f"POST /api/v1/scenario_session_routing/{action}",
                                                _bearer(TOKEN), json.dumps(body))
                assert status == 200 and answer["result"] == {
                    "manager_id": "manager-routing", "session_routing": expected}, answer
                assert await pool.fetchval("SELECT session_routing FROM managers WHERE id='manager-routing'") == expected
            for body in ({"manager_id": "manager", "session_routing": "per_chat"},
                         {"manager_id": "manager-routing", "session_routing": "other"},
                         {"manager_id": "manager-routing", "session_routing": "off", "extra": True}):
                status, _, answer = await _send(server, "POST /api/v1/scenario_session_routing/update",
                                                _bearer(TOKEN), json.dumps(body))
                assert status == 400, answer
        assert await pool.fetchval("SELECT count(*) FROM manager_actions "
                                   "WHERE tool='scenario_session_routing.update' AND status='ok'") == 2
        assert await pool.fetchval("SELECT session_routing FROM managers WHERE id='manager'") == "off"

    run_db(check, monkeypatch)


def test_scenario_routing_confirmation_describes_fresh_sessions_and_return_to_main():
    operation = api_v1.find("POST", "/api/v1/scenario_session_routing/update")
    assert operation is not None

    async def check():
        for value in ("per_chat", "off", None):
            impact = await api_v1.domain_for(operation).impact(None, operation,
                {"manager_id": "manager-routing", "session_routing": value}, api_v1.OPERATOR, {})
            assert "manager-routing" in impact
            assert ("fresh" if value == "per_chat" else "main") in impact
    asyncio.run(check())


def test_generated_cli_carries_typed_routing_and_null(tmp_path, capsys):
    from looper import loopercli
    from tests.test_loopercli import Server, _send as reply

    def respond(handler, request):
        reply(handler, 200, api_v1.document() if request[1].endswith("openapi.json") else {
            "status": "ok", "result": {"manager_id": "manager-routing", "session_routing": "off"}})

    server = Server(respond)
    try:
        token = tmp_path / "operator-token"
        token.write_text(TOKEN)
        token.chmod(0o600)
        base = ["--client-file", str(tmp_path / "client.json"), "--json"]
        for value in ("per_chat", None):
            assert loopercli.main([*base, "scenario-session-routing", "update", "--manager-id", "manager-routing",
                                   "--session-routing", json.dumps(value), "--url", server.url,
                                   "--operator-token-file", str(token)]) == 0
            capsys.readouterr()
        requests = [(request[1], request[3]) for request in server.requests if request[0] == "POST"]
        assert requests == [("/api/v1/scenario_session_routing/update", {
            "manager_id": "manager-routing", "session_routing": value}) for value in ("per_chat", None)]
    finally:
        server.close()
