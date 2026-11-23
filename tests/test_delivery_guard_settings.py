"""Delivery guard configuration stays operator-only and leaves existing chat controls intact."""

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
    monkeypatch.setattr(chat_settings.db_ledger, "enabled", AsyncMock(return_value=False))
    return stored, writes


def test_setting_is_default_off_persisted_loaded_and_reset(settings):
    stored, writes = settings

    async def check():
        await chat_settings.load()
        assert chat_settings.delivery_guard_enabled() is False
        assert await chat_settings.update_delivery_guard(True) == {"enabled": True}
        assert stored[chat_settings.DELIVERY_GUARD_KEY] == "true"
        chat_settings.set_delivery_guard_enabled(False)
        await chat_settings.load()
        assert chat_settings.delivery_guard_enabled() is True
        assert await chat_settings.update_delivery_guard(False) == {"enabled": False}
        assert stored[chat_settings.DELIVERY_GUARD_KEY] == "false"
        await chat_settings.update_delivery_guard(None)
        assert chat_settings.DELIVERY_GUARD_KEY not in stored
        assert chat_settings.delivery_guard_enabled() is False
        stored[chat_settings.DELIVERY_GUARD_KEY] = "garbage"
        await chat_settings.load()
        assert chat_settings.delivery_guard_enabled() is False
        assert len(writes) == 3

    asyncio.run(check())


@pytest.mark.parametrize("value", ["true", 1, [], {}])
def test_invalid_setting_cannot_write_or_change_memory(settings, value):
    async def check():
        with pytest.raises(ValueError, match="true, false or null"):
            await chat_settings.update_delivery_guard(value)
        assert settings == ({}, [])
        assert chat_settings.delivery_guard_enabled() is False
    asyncio.run(check())


def test_default_off_preserves_chat_settings_view_and_writes(settings):
    async def check():
        await chat_settings.load()
        assert await chat_settings.settings_view() == {
            "stop_enabled": False, "completion_turns": "off", "research_order": False, "ledger_enabled": False}
        assert await chat_settings.update({"research_order": False}) == {
            "stop_enabled": False, "completion_turns": "off", "research_order": False, "ledger_enabled": False}
        assert settings[1] == [{"manager.research_order": "false"}]
        with pytest.raises(ValueError, match="unknown setting"):
            chat_settings.checked({"delivery_guard_enabled": True})
    asyncio.run(check())


@pytest.mark.parametrize("action", ["get", "update"])
@pytest.mark.usefixtures("clients")
def test_agent_token_cannot_read_or_change_delivery_guard(tmp_path, monkeypatch, settings, action):
    operation = api_v1.find("POST", f"/api/v1/delivery_guard/{action}")
    assert operation is not None and operation.operator_only and operation.tool is None
    assert all(chat_settings.DELIVERY_GUARD_KEY not in json.dumps(tool.schema) for tool in MANAGER_TOOLS)
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


def test_operator_reads_and_updates_through_v1(tmp_path, monkeypatch, settings):
    from looper.db import manager_actions

    monkeypatch.setattr(manager_actions, "record", AsyncMock(return_value=1))

    async def check():
        await chat_settings.load()
        async with _serving(tmp_path) as server:
            server._client_ops = ClientOps(SimpleNamespace())
            for action, body, expected in (("get", {}, False), ("update", {"enabled": True}, True),
                                           ("get", {}, True), ("update", {"enabled": None}, False)):
                status, _, answer = await _send(server, f"POST /api/v1/delivery_guard/{action}",
                                                _bearer(TOKEN), json.dumps(body))
                assert status == 200 and answer["result"] == {"enabled": expected}, answer
    asyncio.run(check())


@pytest.mark.parametrize("value,expected", [(True, "true"), (False, "false"), (None, "false")])
def test_update_declares_high_risk_confirmation(settings, value, expected):
    operation = api_v1.find("POST", "/api/v1/delivery_guard/update")
    assert operation.risk == "high" and operation.confirms

    async def check():
        impact = await api_v1.domain_for(operation).impact(
            None, operation, {"enabled": value}, api_v1.OPERATOR, {})
        assert f"manager.delivery_guard.enabled to {expected}" in impact
        assert settings == ({}, [])
    asyncio.run(check())


@pytest.mark.parametrize("body", [{"session_routing_enabled": True}, {"max_resident": 4}])
@pytest.mark.usefixtures("clients")
def test_chat_settings_reject_session_routing_without_writing(tmp_path, monkeypatch, settings, body):
    from looper.db import manager_actions

    monkeypatch.setattr(manager_actions, "record", AsyncMock(return_value=1))

    async def check():
        with pytest.raises(ValueError, match="unknown setting"):
            await chat_settings.update({"stop_enabled": True, **body})
        assert settings == ({}, [])
        assert chat_settings.stop_enabled() is False
        async with _serving(tmp_path) as server:
            server._client_ops = ClientOps(SimpleNamespace())
            for token in (TOKEN, CLIENT_TOKEN):
                status, _, answer = await _send(server, "POST /api/v1/chat_settings/update", _bearer(token),
                                                json.dumps({"stop_enabled": True, **body}))
                assert status == 400 and answer["error"]["code"] == "invalid"
                assert "unknown setting" in answer["error"]["message"]
                assert settings == ({}, [])
    asyncio.run(check())


@pytest.mark.parametrize("action", ["get", "update"])
@pytest.mark.usefixtures("clients")
def test_session_routing_operations_are_operator_only(tmp_path, monkeypatch, settings, action):
    from looper.agents.action_levels import ActionLevel

    operation = api_v1.find("POST", f"/api/v1/session_routing/{action}")
    assert operation is not None and operation.operator_only and operation.tool is None
    assert operation.level is ActionLevel.INSIDE
    assert operation.risk == ("high" if action == "update" else "read")
    assert operation.confirms is (action == "update")
    run = AsyncMock(side_effect=AssertionError("client reached session routing settings"))
    monkeypatch.setattr(api_v1.domain_for(operation), "run", run)

    async def check():
        async with _serving(tmp_path) as server:
            server._client_ops = ClientOps(SimpleNamespace())
            status, _, answer = await _send(server, f"POST {operation.path}", _bearer(CLIENT_TOKEN),
                                            json.dumps({"session_routing_enabled": True} if action == "update" else {}))
            assert status == 403 and answer["error"]["code"] == "forbidden"
            run.assert_not_awaited()
            assert settings[1] == []
    asyncio.run(check())
