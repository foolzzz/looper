"""Manager lanes are an operator-only, default-off system setting."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from looper.agents import api_v1, chat_settings, lanes_settings
from looper.agents.action_levels import ActionLevel
from looper.agents.ops.clients import ClientOps
from looper.agents.ops.service import MANAGER_TOOLS
from tests.test_api_v1_auth import CLIENT_TOKEN, _bearer, _send, _serving, clients  # noqa: F401
from tests.test_operator_auth import TOKEN


@pytest.fixture
def settings(monkeypatch):
    stored, writes = {}, []
    monkeypatch.setattr(lanes_settings, "_enabled", False)

    async def read(keys):
        return {key: stored[key] for key in keys if key in stored}

    async def write(values):
        writes.append(values)
        for key, value in values.items():
            if value is None:
                stored.pop(key, None)
            else:
                stored[key] = value

    monkeypatch.setattr(lanes_settings.db_settings, "get_settings", read)
    monkeypatch.setattr(lanes_settings.db_settings, "put_settings", write)
    return stored, writes


def test_default_off_persist_load_and_reset(settings):
    stored, writes = settings

    async def check():
        await lanes_settings.load()
        assert lanes_settings.view() == {"enabled": False}
        assert await lanes_settings.update(True) == {"enabled": True}
        assert stored == {"manager.lanes.enabled": "true"}
        lanes_settings.set_enabled(False)
        await lanes_settings.load()
        assert lanes_settings.enabled() is True
        assert await lanes_settings.update(False) == {"enabled": False}
        assert stored == {"manager.lanes.enabled": "false"}
        assert await lanes_settings.update(None) == {"enabled": False}
        assert stored == {}
        assert len(writes) == 3

    asyncio.run(check())


@pytest.mark.parametrize("value", [None, "false", "TRUE", "1", "garbage", " true"])
def test_only_exact_true_is_on(settings, value):
    async def check():
        if value is not None:
            settings[0][lanes_settings.KEY] = value
        lanes_settings.set_enabled(True)
        await lanes_settings.load()
        assert lanes_settings.enabled() is False

    asyncio.run(check())


@pytest.mark.parametrize("value", ["true", "false", 0, 1, [], {}])
def test_invalid_update_never_writes(settings, value):
    async def check():
        with pytest.raises(ValueError, match="true, false or null"):
            await lanes_settings.update(value)
        assert settings == ({}, [])
        assert lanes_settings.enabled() is False

    asyncio.run(check())


def test_failed_write_does_not_change_memory(settings, monkeypatch):
    monkeypatch.setattr(lanes_settings.db_settings, "put_settings", AsyncMock(side_effect=RuntimeError("offline")))

    async def check():
        with pytest.raises(RuntimeError, match="offline"):
            await lanes_settings.update(True)
        assert lanes_settings.enabled() is False

    asyncio.run(check())


@pytest.mark.parametrize("action", ["get", "update"])
@pytest.mark.usefixtures("clients")
def test_client_token_is_forbidden(tmp_path, monkeypatch, settings, action):
    operation = api_v1.find("POST", f"/api/v1/manager_lanes/{action}")
    assert operation is not None and operation.operator_only and operation.tool is None
    assert operation.level is ActionLevel.INSIDE
    assert operation.risk == ("high" if action == "update" else "read")
    assert operation.confirms is (action == "update")
    run = AsyncMock(side_effect=AssertionError("client reached Manager lanes"))
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


def test_operator_can_get_enable_and_reset(tmp_path, monkeypatch, settings):
    from looper.db import manager_actions

    monkeypatch.setattr(manager_actions, "record", AsyncMock(return_value=1))

    async def check():
        async with _serving(tmp_path) as server:
            server._client_ops = ClientOps(SimpleNamespace())
            for action, body, expected in (("get", {}, False), ("update", {"enabled": True}, True),
                                           ("get", {}, True), ("update", {"enabled": None}, False)):
                status, _, answer = await _send(server, f"POST /api/v1/manager_lanes/{action}",
                                                _bearer(TOKEN), json.dumps(body))
                assert status == 200 and answer["result"] == {"enabled": expected}, answer

    asyncio.run(check())


def test_switch_is_separate_from_chat_settings_and_tools(settings):
    for name in ("enabled", "manager_lanes_enabled", "manager.lanes.enabled"):
        with pytest.raises(ValueError, match="unknown setting"):
            chat_settings.checked({name: True})
    assert all("manager.lanes" not in json.dumps(tool.schema) for tool in MANAGER_TOOLS)


def test_confirmation_states_the_rfc_behavior(settings):
    operation = api_v1.find("POST", "/api/v1/manager_lanes/update")

    async def check():
        text = await api_v1.domain_for(operation).impact(
            None, operation, {"enabled": True}, api_v1.OPERATOR, {})
        assert text == ("Set manager.lanes.enabled to true? "
                        "打开后，你的消息到达时，正在进行的收尾回合、空闲检查会被中断"
                        "（收尾改发代码通知），你的消息先处理。")
        assert settings == ({}, [])

    asyncio.run(check())


@pytest.mark.parametrize("entry", [{}, {"enabled": True}, {"enabled": None}])
def test_config_import_cannot_bypass_operator_only_setting(entry):
    from looper.agents import config_imports
    from looper.agents.ops import ErrorCode, OpError

    async def check():
        document = {"settings": {"manager_lanes": entry}}
        with pytest.raises(OpError) as caught:
            await config_imports.authorize(document, "client")
        assert caught.value.code is ErrorCode.FORBIDDEN
        assert await config_imports.authorize(document, "operator") == document

    asyncio.run(check())
