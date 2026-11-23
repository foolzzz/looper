"""Mid-turn input has a default-off, operator-only configuration contract."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from looper.agents import api_v1, chat_settings
from looper.agents.action_levels import ActionLevel
from looper.agents.ops.clients import ClientOps
from looper.agents.ops.service import MANAGER_TOOLS
from tests.test_api_v1_auth import CLIENT_TOKEN, _bearer, _send, _serving, clients  # noqa: F401
from tests.test_operator_auth import TOKEN


@pytest.fixture
def settings(monkeypatch):
    stored, writes = {}, []

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
    monkeypatch.setattr(chat_settings, "_mid_turn_input_enabled", False, raising=False)
    monkeypatch.setattr(chat_settings.db_ledger, "enabled", AsyncMock(return_value=False))
    return stored, writes


def test_setting_defaults_off_loads_persists_and_resets(settings):
    stored, writes = settings

    async def check():
        await chat_settings.load()
        assert chat_settings.mid_turn_input_enabled() is False
        assert await chat_settings.update_mid_turn_input(True) == {"enabled": True}
        assert stored[chat_settings.MID_TURN_INPUT_KEY] == "true"
        chat_settings.set_mid_turn_input_enabled(False)
        await chat_settings.load()
        assert chat_settings.mid_turn_input_enabled() is True
        assert await chat_settings.update_mid_turn_input(False) == {"enabled": False}
        assert stored[chat_settings.MID_TURN_INPUT_KEY] == "false"
        assert await chat_settings.update_mid_turn_input(None) == {"enabled": False}
        assert chat_settings.MID_TURN_INPUT_KEY not in stored
        stored[chat_settings.MID_TURN_INPUT_KEY] = "garbage"
        await chat_settings.load()
        assert chat_settings.mid_turn_input_enabled() is False
        assert len(writes) == 3

    asyncio.run(check())


@pytest.mark.parametrize("value", ["true", 1, [], {}])
def test_invalid_value_changes_nothing(settings, value):
    async def check():
        with pytest.raises(ValueError, match="true, false or null"):
            await chat_settings.update_mid_turn_input(value)
        assert settings == ({}, [])
        assert chat_settings.mid_turn_input_enabled() is False
    asyncio.run(check())


def test_failed_persistence_does_not_enable_merging(monkeypatch, settings):
    monkeypatch.setattr(chat_settings.db_settings, "put_settings", AsyncMock(side_effect=OSError("unavailable")))

    async def check():
        with pytest.raises(OSError, match="unavailable"):
            await chat_settings.update_mid_turn_input(True)
        assert chat_settings.mid_turn_input_enabled() is False
    asyncio.run(check())


def test_chat_controls_and_manager_tools_do_not_expose_the_setting(settings):
    async def check():
        await chat_settings.load()
        expected = {"stop_enabled": False, "completion_turns": "off", "research_order": False,
                    "ledger_enabled": False}
        assert await chat_settings.settings_view() == expected
        assert await chat_settings.update({"research_order": False}) == expected
        assert settings[1] == [{"manager.research_order": "false"}]
        with pytest.raises(ValueError, match="unknown setting"):
            await chat_settings.update({"mid_turn_input_enabled": True})
        assert all(chat_settings.MID_TURN_INPUT_KEY not in json.dumps(tool.schema) for tool in MANAGER_TOOLS)
    asyncio.run(check())


@pytest.mark.parametrize("action", ["get", "update"])
@pytest.mark.usefixtures("clients")
def test_client_token_cannot_read_or_change_mid_turn_input(tmp_path, monkeypatch, settings, action):
    operation = api_v1.find("POST", f"/api/v1/mid_turn_input/{action}")
    assert operation is not None and operation.operator_only and operation.tool is None
    assert operation.level is ActionLevel.INSIDE
    run = AsyncMock(side_effect=AssertionError("client reached operator setting"))
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


def test_operator_can_read_enable_and_reset_through_v1(tmp_path, monkeypatch, settings):
    from looper.db import manager_actions

    monkeypatch.setattr(manager_actions, "record", AsyncMock(return_value=1))

    async def check():
        await chat_settings.load()
        async with _serving(tmp_path) as server:
            server._client_ops = ClientOps(SimpleNamespace())
            for action, body, expected in (("get", {}, False), ("update", {"enabled": True}, True),
                                           ("get", {}, True), ("update", {"enabled": None}, False)):
                status, _, answer = await _send(server, f"POST /api/v1/mid_turn_input/{action}",
                                                _bearer(TOKEN), json.dumps(body))
                assert status == 200 and answer["result"] == {"enabled": expected}, answer
    asyncio.run(check())


@pytest.mark.parametrize("value,expected", [(True, "true"), (False, "false"), (None, "false")])
def test_update_is_high_risk_with_an_explicit_impact(settings, value, expected):
    operation = api_v1.find("POST", "/api/v1/mid_turn_input/update")
    assert operation.risk == "high" and operation.confirms

    async def check():
        impact = await api_v1.domain_for(operation).impact(
            None, operation, {"enabled": value}, api_v1.OPERATOR, {})
        assert f"manager.mid_turn_input.enabled to {expected}" in impact
        assert "running" in impact
        assert settings == ({}, [])
    asyncio.run(check())
