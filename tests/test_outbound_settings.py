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
        assert (await chat_settings.update({'outbound_enabled': True}))['outbound_enabled'] is True
        assert stored == {'channels.outbound.enabled': 'true'}
        chat_settings.set_outbound_enabled(False)
        await chat_settings.load()
        assert chat_settings.outbound_enabled() is True
        with pytest.raises(ValueError, match='outbound_enabled must be'):
            await chat_settings.update({'outbound_enabled': 'true'})
        assert chat_settings.outbound_enabled() is True
        assert (await chat_settings.update({'outbound_enabled': None}))['outbound_enabled'] is False
        assert not stored

    asyncio.run(run())


def test_outbound_settings_are_operator_only():
    for name in ('get', 'update'):
        assert api_v1.find('POST', '/api/v1/outbound_settings/' + name).operator_only


@pytest.mark.parametrize('operation,body', [
    ('outbound_settings.get', {}),
    ('outbound_settings.update', {'outbound_enabled': True}),
    ('chat_settings.update', {'outbound_enabled': True}),
    ('outbound_settings.update', {'send_qps': 2}),
    ('chat_settings.update', {'max_in_flight': 2}),
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
                for operation, body in (("outbound_settings/get", {}),
                                        ("outbound_settings/update", {"outbound_enabled": True}),
                                        ("chat_settings/update", {"outbound_enabled": True})):
                    response = await client.post("/api/v1/" + operation, json=body)
                    assert response.status_code == 403
                    assert response.json()["error"]["code"] == "forbidden"
        finally:
            await server.stop()

    asyncio.run(run())


def test_outbound_limits_defaults_update_reload_reset_and_schema(monkeypatch):
    from looper.agents.v1_domains import chat_controls
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
        operation = api_v1.find('POST', '/api/v1/outbound_settings/update')
        values = dict(send_qps=2.5, write_qps=7, edit_interval_seconds=.25, max_in_flight=2)
        assert set(values) <= operation.request['properties'].keys()
        await chat_controls.run(None, operation, values, api_v1.Principal('operator'), {})
        assert chat_settings.outbound_limits() == values
        assert stored == {'channels.outbound.' + k: str(v) for k, v in values.items()}
        await chat_settings.load()
        assert chat_settings.outbound_limits() == values
        await chat_settings.update(dict.fromkeys(defaults))
        assert chat_settings.outbound_limits() == defaults and not stored
    asyncio.run(run())


@pytest.mark.parametrize('field,value', [
    ('send_qps', 0), ('send_qps', True), ('write_qps', float('inf')),
    ('edit_interval_seconds', float('nan')), ('max_in_flight', 0),
    ('max_in_flight', 33), ('max_in_flight', 1.5), ('max_in_flight', True),
])
def test_outbound_limits_reject_invalid_values(field, value):
    with pytest.raises(ValueError, match=field + " must be"):
        chat_settings.checked({field: value})
