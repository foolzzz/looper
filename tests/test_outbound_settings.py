"""Outbound channel control is off by default and belongs to the operator."""

import asyncio
from unittest.mock import AsyncMock

import pytest

from looper.agents import api_v1, chat_settings


def test_outbound_setting_load_update_reset_and_validation(monkeypatch):
    stored = {}

    async def put(values):
        for key, value in values.items():
            if value is None:
                stored.pop(key, None)
            else:
                stored[key] = value

    monkeypatch.setattr(chat_settings.db_settings, 'get_settings', AsyncMock(side_effect=lambda _: stored))
    monkeypatch.setattr(chat_settings.db_settings, 'put_settings', put)
    monkeypatch.setattr(chat_settings.db_ledger, 'enabled', AsyncMock(return_value=False))
    monkeypatch.setattr(chat_settings, '_outbound_enabled', False, raising=False)

    async def run():
        await chat_settings.load()
        assert chat_settings.outbound_enabled() is False
        assert (await chat_settings.update_outbound({'outbound_enabled': True}))['outbound_enabled'] is True
        assert stored == {'channels.outbound.enabled': 'true'}
        chat_settings.set_outbound_enabled(False)
        await chat_settings.load()
        assert chat_settings.outbound_enabled() is True
        with pytest.raises(ValueError, match='outbound_enabled must be'):
            await chat_settings.update_outbound({'outbound_enabled': 'true'})
        assert chat_settings.outbound_enabled() is True
        assert (await chat_settings.update_outbound({'outbound_enabled': None}))['outbound_enabled'] is False
        assert not stored

    asyncio.run(run())


def test_outbound_gateway_are_operator_only_and_inside():
    from looper.agents.action_levels import ActionLevel

    for name in ('get', 'update'):
        operation = api_v1.find('POST', '/api/v1/outbound_gateway/' + name)
        assert operation.operator_only and operation.tool is None
        assert operation.level is ActionLevel.INSIDE and operation.level_of is None


@pytest.mark.parametrize('operation,body', [
    ('outbound_gateway.get', {}),
    ('outbound_gateway.update', {'outbound_enabled': True}),
    ('outbound_gateway.update', {'send_qps': 2}),
])
@pytest.mark.parametrize('principal_kind', ['client', 'agent'])
def test_nonoperator_principal_cannot_change_outbound_control(operation, body, tmp_path, principal_kind):
    import json
    from types import SimpleNamespace

    from looper.agents.http_server import AgentHTTPServer
    from looper.agents.ops.clients import ClientOps

    class Writer:
        def __init__(self):
            self.data = b""

        def write(self, data):
            self.data += data

        async def drain(self):
            pass

    async def run():
        server = AgentHTTPServer(web_adapter=SimpleNamespace(), supervisor=SimpleNamespace(),
                                 task_router=SimpleNamespace())
        server._client_ops = ClientOps(SimpleNamespace())
        writer = Writer()
        await server._handle_v1('POST', '/api/v1/' + operation.replace('.', '/'), {}, {},
                                json.dumps(body).encode(), writer, api_v1.Principal(principal_kind, 'synthetic', 'test'))
        head, _, payload = writer.data.partition(b'\r\n\r\n')
        assert int(head.split()[1]) == 403
        assert json.loads(payload)['error']['code'] == 'forbidden'

    asyncio.run(run())



def test_http_agent_client_token_is_forbidden_and_settings_tool_omits_outbound(tmp_path, monkeypatch):
    import json
    from types import SimpleNamespace

    import httpx

    from looper.agents.http_server import AgentHTTPServer
    from looper.agents.ops import settings
    from looper.agents.ops.clients import ClientOps
    from looper.db import api_clients

    token = "lpc_" + "s" * 43
    monkeypatch.setattr(api_clients, "find", AsyncMock(return_value={"id": "client-synthetic", "name": "test agent"}))
    monkeypatch.setattr(api_clients, "touch", AsyncMock())
    tool = next(tool for tool in settings.TOOLS if tool.name == "looper_settings")
    assert "outbound" not in json.dumps(tool.schema)
    assert "outbound" not in tool.description

    async def run():
        server = AgentHTTPServer(host="127.0.0.1", port=0, operator_token="synthetic-operator",
                                 static_dir=tmp_path, runtime_root=tmp_path,
                                 web_adapter=SimpleNamespace(), supervisor=SimpleNamespace(),
                                 task_router=SimpleNamespace())
        server._client_ops = ClientOps(SimpleNamespace())
        await server.start()
        try:
            async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{server._port}",
                                         headers={"Authorization": "Bearer " + token},
                                         limits=httpx.Limits(max_keepalive_connections=0)) as client:
                # Positive control: this is an authenticated API client, not an invalid token.
                response = await client.get("/api/v1/whoami")
                assert response.status_code == 200 and response.json()["result"]["principal"] == "client"
                for operation, body in (("outbound_gateway/get", {}),
                                        ("outbound_gateway/update", {"outbound_enabled": True})):
                    response = await client.post("/api/v1/" + operation, json=body)
                    assert response.status_code == 403
                    assert response.json()["error"]["code"] == "forbidden"
        finally:
            await server.stop()

    asyncio.run(run())


def test_outbound_limits_defaults_update_reload_reset_and_schema(monkeypatch):
    from looper.agents.v1_domains import outbound_gateway
    stored = {}
    async def put(values):
        for key, value in values.items():
            if value is None:
                stored.pop(key, None)
            else:
                stored[key] = value
    monkeypatch.setattr(chat_settings.db_settings, 'get_settings', AsyncMock(side_effect=lambda _: stored))
    monkeypatch.setattr(chat_settings.db_settings, 'put_settings', put)
    monkeypatch.setattr(chat_settings.db_ledger, 'enabled', AsyncMock(return_value=False))
    monkeypatch.setattr(chat_settings, '_outbound_limits', {}, raising=False)
    async def run():
        await chat_settings.load()
        defaults = dict(send_qps=5, write_qps=15, edit_interval_seconds=1, max_in_flight=4)
        assert chat_settings.outbound_limits() == defaults
        operation = api_v1.find('POST', '/api/v1/outbound_gateway/update')
        values = dict(send_qps=2.5, write_qps=7, edit_interval_seconds=.25, max_in_flight=2)
        assert set(values) <= operation.request['properties'].keys()
        await outbound_gateway.run(None, operation, values, api_v1.Principal('operator'), {})
        assert chat_settings.outbound_limits() == values
        assert stored == {'channels.outbound.' + k: str(v) for k, v in values.items()}
        await chat_settings.load()
        assert chat_settings.outbound_limits() == values
        await chat_settings.update_outbound(dict.fromkeys(defaults))
        assert chat_settings.outbound_limits() == defaults and not stored
    asyncio.run(run())


@pytest.mark.parametrize('field,value', [
    ('send_qps', 0), ('send_qps', True), ('write_qps', float('inf')),
    ('edit_interval_seconds', float('nan')), ('max_in_flight', 0),
    ('max_in_flight', 33), ('max_in_flight', 1.5), ('max_in_flight', True),
])
def test_outbound_limits_reject_invalid_values(field, value):
    with pytest.raises(ValueError, match=field + " must be"):
        chat_settings.checked_outbound({field: value})


def test_outbound_controls_do_not_extend_chat_settings(monkeypatch):
    writes = AsyncMock()
    monkeypatch.setattr(chat_settings.db_settings, "put_settings", writes)
    fields = {"stop_enabled", "completion_turns", "research_order", "ledger_enabled"}
    for action in ("get", "update"):
        operation = api_v1.find("POST", "/api/v1/chat_settings/" + action)
        assert set(operation.result["properties"]) == fields | ({"missing_operator_ids"} if action == "update" else set())
        if action == "update":
            assert set(operation.request["properties"]) == fields

    async def run():
        for key in chat_settings.OUTBOUND_FIELDS:
            with pytest.raises(ValueError, match="unknown setting"):
                await chat_settings.update({"stop_enabled": True, key: True})
        with pytest.raises(ValueError, match="unknown outbound setting"):
            await chat_settings.update_outbound({"outbound_enabled": True, "ledger_enabled": True})
        with pytest.raises(ValueError, match="at least one"):
            await chat_settings.update_outbound({})
        writes.assert_not_awaited()
    asyncio.run(run())


def test_outbound_update_requires_confirmation_without_writing(monkeypatch):
    operation = api_v1.find("POST", "/api/v1/outbound_gateway/update")
    assert operation.risk == "high" and operation.confirms and operation.tool is None
    writes = AsyncMock()
    monkeypatch.setattr(chat_settings.db_settings, "put_settings", writes)

    async def run():
        impact = await api_v1.domain_for(operation).impact(
            None, operation, {"outbound_enabled": True, "send_qps": None}, api_v1.OPERATOR, {})
        assert "channels.outbound.enabled to true" in impact
        assert "channels.outbound.send_qps to 5" in impact
        writes.assert_not_awaited()
    asyncio.run(run())


def test_gateway_updates_preserve_metrics_and_turn_context(monkeypatch):
    stored = {chat_settings.METRICS_ENABLED_KEY: "true", chat_settings.TURN_CONTEXT_KEY: "true",
              chat_settings.TIMEZONE_KEY: "Asia/Shanghai", chat_settings.REPLY_LANGUAGE_KEY: "en"}

    async def get(keys):
        return {key: stored[key] for key in keys if key in stored}

    async def put(values):
        for key, value in values.items():
            if value is None:
                stored.pop(key, None)
            else:
                stored[key] = value

    monkeypatch.setattr(chat_settings.db_settings, "get_settings", get)
    monkeypatch.setattr(chat_settings.db_settings, "put_settings", put)
    for name in ("_metrics_enabled", "_turn_context_enabled", "_outbound_enabled"):
        monkeypatch.setattr(chat_settings, name, False)
    monkeypatch.setattr(chat_settings, "_timezone", None)
    monkeypatch.setattr(chat_settings, "_reply_language", "zh-CN")
    monkeypatch.setattr(chat_settings, "_outbound_limits", dict(chat_settings.OUTBOUND_DEFAULTS))

    async def run():
        await chat_settings.update_outbound({"outbound_enabled": True, "send_qps": 2})
        assert chat_settings.metrics_enabled() and chat_settings.turn_context_enabled()
        assert chat_settings.timezone() == "Asia/Shanghai" and chat_settings.reply_language() == "en"
        await chat_settings.update_metrics(False)
        await chat_settings.update_turn_context({"turn_context_enabled": False})
        assert chat_settings.outbound_enabled() and chat_settings.outbound_limits()["send_qps"] == 2
        await chat_settings.update_outbound({"outbound_enabled": None, "send_qps": None})
        assert not chat_settings.metrics_enabled() and not chat_settings.turn_context_enabled()
        assert chat_settings.timezone() == "Asia/Shanghai" and chat_settings.reply_language() == "en"

    asyncio.run(run())
