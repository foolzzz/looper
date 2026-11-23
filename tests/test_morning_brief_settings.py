"""The morning brief's switch (``morning_brief.enabled``, ``morning_brief.time``) stays operator-only, off by
default, and leaves the chat settings clients see unchanged."""

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


def test_the_switch_is_off_at_09_15_by_default_stored_loaded_and_reset(settings):
    stored, writes = settings

    async def check():
        await chat_settings.load()
        assert chat_settings.morning_brief_view() == {"enabled": False, "time": "09:15"}
        assert await chat_settings.update_morning_brief({"enabled": True}) == {"enabled": True, "time": "09:15"}
        assert stored["morning_brief.enabled"] == "true"
        chat_settings.set_morning_brief_enabled(False)  # a restart
        chat_settings.set_morning_brief_time("09:15")
        await chat_settings.load()
        assert chat_settings.morning_brief_enabled() is True
        assert await chat_settings.update_morning_brief({"time": "15:00"}) == {"enabled": True, "time": "15:00"}
        assert stored["morning_brief.time"] == "15:00"
        await chat_settings.update_morning_brief({"enabled": None, "time": None})
        assert "morning_brief.enabled" not in stored and "morning_brief.time" not in stored
        assert chat_settings.morning_brief_view() == {"enabled": False, "time": "09:15"}
        stored["morning_brief.enabled"] = "garbage"
        stored["morning_brief.time"] = "noon"
        await chat_settings.load()
        assert chat_settings.morning_brief_view() == {"enabled": False, "time": "09:15"}
        assert len(writes) == 3

    asyncio.run(check())


@pytest.mark.parametrize("body", [{"enabled": "true"}, {"enabled": 1}, {"time": "9:15"}, {"time": "24:00"},
                                  {"time": 915}, {"time": "09:60"}, {}])
def test_a_bad_value_writes_nothing(settings, body):
    async def check():
        with pytest.raises(ValueError):
            await chat_settings.update_morning_brief(body)
        assert settings == ({}, [])
        assert chat_settings.morning_brief_view() == {"enabled": False, "time": "09:15"}
    asyncio.run(check())


def test_the_chat_settings_clients_see_are_unchanged(settings):
    async def check():
        await chat_settings.load()
        assert await chat_settings.settings_view() == {
            "stop_enabled": False, "completion_turns": "off", "research_order": False, "ledger_enabled": False}
        with pytest.raises(ValueError, match="unknown setting"):
            chat_settings.checked({"morning_brief": True})
        with pytest.raises(ValueError, match="unknown setting"):
            await chat_settings.update_morning_brief({"morning_brief": True})
        assert settings == ({}, [])
    asyncio.run(check())


@pytest.mark.parametrize("action", ["get", "update"])
@pytest.mark.usefixtures("clients")
def test_an_agent_token_cannot_read_or_change_it(tmp_path, monkeypatch, settings, action):
    operation = api_v1.find("POST", f"/api/v1/morning_brief/{action}")
    assert operation is not None and operation.operator_only and operation.tool is None
    assert operation.level is ActionLevel.INSIDE
    dumped = [json.dumps(tool.schema) for tool in MANAGER_TOOLS]
    assert all("morning_brief" not in text for text in dumped)
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
            steps = (("get", {}, {"enabled": False, "time": "09:15"}),
                     ("update", {"enabled": True}, {"enabled": True, "time": "09:15"}),
                     ("update", {"time": "15:00"}, {"enabled": True, "time": "15:00"}),
                     ("update", {"enabled": None, "time": None}, {"enabled": False, "time": "09:15"}))
            for action, body, expected in steps:
                status, _, answer = await _send(server, f"POST /api/v1/morning_brief/{action}",
                                                _bearer(TOKEN), json.dumps(body))
                assert status == 200 and answer["result"] == expected, answer
            for body in ({}, {"time": "9:15"}, {"enabled": "yes"}):
                status, _, answer = await _send(server, "POST /api/v1/morning_brief/update", _bearer(TOKEN),
                                                json.dumps(body))
                assert status == 400, answer
    asyncio.run(check())


@pytest.mark.parametrize(("body", "enabled", "when"), [
    ({"enabled": True}, "true", "09:15"),
    ({"enabled": False}, "false", "09:15"),
    ({"enabled": None}, "false", "09:15"),
    ({"time": "15:00"}, "false", "15:00"),
    ({"time": None}, "false", "09:15"),
])
def test_turning_it_on_or_off_asks_first(settings, body, enabled, when):
    operation = api_v1.find("POST", "/api/v1/morning_brief/update")
    assert operation.risk == "high" and operation.confirms and operation.level is ActionLevel.INSIDE

    async def check():
        impact = await api_v1.domain_for(operation).impact(None, operation, body, api_v1.OPERATOR, {})
        assert f"morning_brief.enabled to {enabled}" in impact
        assert f"morning_brief.time to {when}" in impact
        assert settings == ({}, [])
    asyncio.run(check())
