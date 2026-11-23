"""The delivery layer's switch (``delivery.enabled``) stays operator-only, off by default, and leaves the chat settings
clients see unchanged."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from looper.agents import api_v1, chat_settings
from looper.agents.ops.clients import ClientOps
from looper.agents.ops.service import MANAGER_TOOLS
from tests.test_api_v1_auth import CLIENT_TOKEN, _bearer, _send, _serving, clients  # noqa: F401
from tests.test_operator_auth import TOKEN


@pytest.fixture
def settings(monkeypatch):
    stored = {}
    writes = []
    for name, value in (("_stop_enabled", False), ("_completion_turns", chat_settings.CompletionTurns.OFF),
                        ("_research_order", False)):
        monkeypatch.setattr(chat_settings, name, value)

    async def read(keys):
        return {key: stored[key] for key in keys if key in stored}

    async def write(values):
        writes.append(values)
        for key, value in values.items():
            if value is None:
                stored.pop(key, None)
            else:
                stored[key] = value

    monkeypatch.setattr(chat_settings.db_settings, "get_settings", read)
    monkeypatch.setattr(chat_settings.db_settings, "put_settings", write)
    monkeypatch.setattr(chat_settings, "_delivery_guard_enabled", False, raising=False)
    monkeypatch.setattr(chat_settings, "_delivery_enabled", False)
    monkeypatch.setattr(chat_settings.db_ledger, "enabled", AsyncMock(return_value=False))
    return stored, writes




def test_the_switch_is_off_by_default_stored_loaded_and_reset(settings):
    stored, writes = settings

    async def check():
        await chat_settings.load()
        assert chat_settings.delivery_enabled() is False
        assert await chat_settings.update_delivery(True) == {"enabled": True}
        assert stored["delivery.enabled"] == "true"
        chat_settings.set_delivery_enabled(False)  # a restart
        await chat_settings.load()
        assert chat_settings.delivery_enabled() is True
        await chat_settings.update_delivery(None)
        assert "delivery.enabled" not in stored and chat_settings.delivery_enabled() is False
        stored["delivery.enabled"] = "garbage"
        await chat_settings.load()
        assert chat_settings.delivery_enabled() is False
        assert len(writes) == 2

    asyncio.run(check())


@pytest.mark.parametrize("value", ["true", 1, [], {}])
def test_a_bad_value_writes_nothing(settings, value):
    async def check():
        with pytest.raises(ValueError, match="true, false or null"):
            await chat_settings.update_delivery(value)
        assert settings == ({}, [])
        assert chat_settings.delivery_enabled() is False
    asyncio.run(check())


def test_the_chat_settings_clients_see_are_unchanged(settings):
    async def check():
        await chat_settings.load()
        assert await chat_settings.settings_view() == {
            "stop_enabled": False, "completion_turns": "off", "research_order": False, "ledger_enabled": False}
        with pytest.raises(ValueError, match="unknown setting"):
            chat_settings.checked({"delivery_enabled": True})
    asyncio.run(check())


@pytest.mark.parametrize("action", ["get", "update"])
@pytest.mark.usefixtures("clients")
def test_an_agent_token_cannot_read_or_change_it(tmp_path, monkeypatch, settings, action):
    operation = api_v1.find("POST", f"/api/v1/proactive_delivery/{action}")
    assert operation is not None and operation.operator_only and operation.tool is None
    assert all("delivery.enabled" not in json.dumps(tool.schema) for tool in MANAGER_TOOLS)
    run = AsyncMock(side_effect=AssertionError("agent token reached the setting"))
    monkeypatch.setattr(api_v1.domain_for(operation), "run", run)

    async def check():
        async with _serving(tmp_path) as server:
            server._client_ops = ClientOps(SimpleNamespace())
            status, _, answer = await _send(server, f"POST {operation.path}", _bearer(CLIENT_TOKEN),
                                            json.dumps({"enabled": True} if action == "update" else {}))
            assert status == 403 and answer["error"]["code"] == "forbidden"
            run.assert_not_awaited()
            assert settings[1] == []
    asyncio.run(check())


def test_the_operator_reads_and_changes_it_through_v1(tmp_path, monkeypatch, settings):
    from looper.db import manager_actions

    monkeypatch.setattr(manager_actions, "record", AsyncMock(return_value=1))

    async def check():
        await chat_settings.load()
        async with _serving(tmp_path) as server:
            server._client_ops = ClientOps(SimpleNamespace())
            for action, body, expected in (("get", {}, False), ("update", {"enabled": True}, True),
                                           ("get", {}, True), ("update", {"enabled": None}, False)):
                status, _, answer = await _send(server, f"POST /api/v1/proactive_delivery/{action}",
                                                _bearer(TOKEN), json.dumps(body))
                assert status == 200 and answer["result"] == {"enabled": expected}, answer
            status, _, answer = await _send(server, "POST /api/v1/proactive_delivery/update", _bearer(TOKEN),
                                            json.dumps({"enabled": "yes"}))
            assert status == 400
    asyncio.run(check())


@pytest.mark.parametrize("value,expected", [(True, "true"), (False, "false"), (None, "false")])
def test_turning_it_on_or_off_asks_first(settings, value, expected):
    operation = api_v1.find("POST", "/api/v1/proactive_delivery/update")
    assert operation.risk == "high" and operation.confirms

    async def check():
        impact = await api_v1.domain_for(operation).impact(None, operation, {"enabled": value}, api_v1.OPERATOR, {})
        assert f"delivery.enabled to {expected}" in impact
        assert settings == ({}, [])
    asyncio.run(check())
