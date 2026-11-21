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
        assert chat_settings.settings_view() == {
            "stop_enabled": False, "completion_turns": "off", "research_order": False}
        assert await chat_settings.update({"research_order": False}) == {
            "stop_enabled": False, "completion_turns": "off", "research_order": False}
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
