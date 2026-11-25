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
        assert chat_settings.morning_brief_view() == {"enabled": False, "time": "09:15", "cutoff": "12:00", "cutoff_source": "derived"}
        assert await chat_settings.update_morning_brief({"enabled": True}) == {"enabled": True, "time": "09:15", "cutoff": "12:00", "cutoff_source": "derived"}
        assert stored["morning_brief.enabled"] == "true"
        chat_settings.set_morning_brief_enabled(False)  # a restart
        chat_settings.set_morning_brief_time("09:15")
        await chat_settings.load()
        assert chat_settings.morning_brief_enabled() is True
        assert await chat_settings.update_morning_brief({"time": "15:00", "cutoff": "16:00"}) == {"enabled": True, "time": "15:00", "cutoff": "16:00", "cutoff_source": "stored"}
        assert stored["morning_brief.time"] == "15:00"
        await chat_settings.update_morning_brief({"enabled": None, "time": None, "cutoff": None})
        assert "morning_brief.enabled" not in stored and "morning_brief.time" not in stored
        assert chat_settings.morning_brief_view() == {"enabled": False, "time": "09:15", "cutoff": "12:00", "cutoff_source": "derived"}
        stored["morning_brief.enabled"] = "garbage"
        stored["morning_brief.time"] = "noon"
        stored["morning_brief.cutoff"] = "noon"
        await chat_settings.load()
        assert chat_settings.morning_brief_view() == {"enabled": False, "time": "09:15", "cutoff": "12:00", "cutoff_source": "derived"}
        assert len(writes) == 3

    asyncio.run(check())


@pytest.mark.parametrize("body", [{"enabled": "true"}, {"enabled": 1}, {"time": "9:15"}, {"time": "24:00"},
                                  {"time": 915}, {"time": "09:60"}, {}])
def test_a_bad_value_writes_nothing(settings, body):
    async def check():
        with pytest.raises(ValueError):
            await chat_settings.update_morning_brief(body)
        assert settings == ({}, [])
        assert chat_settings.morning_brief_view() == {"enabled": False, "time": "09:15", "cutoff": "12:00", "cutoff_source": "derived"}
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
            steps = (("get", {}, {"enabled": False, "time": "09:15", "cutoff": "12:00", "cutoff_source": "derived"}),
                     ("update", {"enabled": True}, {"enabled": True, "time": "09:15", "cutoff": "12:00", "cutoff_source": "derived"}),
                     ("update", {"time": "15:00", "cutoff": "16:00"}, {"enabled": True, "time": "15:00", "cutoff": "16:00", "cutoff_source": "stored"}),
                     ("update", {"cutoff": None}, {"enabled": True, "time": "15:00", "cutoff": "18:00", "cutoff_source": "derived"}),
                     ("get", {}, {"enabled": True, "time": "15:00", "cutoff": "18:00", "cutoff_source": "derived"}),
                     ("update", {"time": "23:59"}, {"enabled": True, "time": "23:59", "cutoff": {"kind": "end_of_day"}, "cutoff_source": "derived"}),
                     ("get", {}, {"enabled": True, "time": "23:59", "cutoff": {"kind": "end_of_day"}, "cutoff_source": "derived"}),
                     ("update", {"enabled": None, "time": None, "cutoff": None}, {"enabled": False, "time": "09:15", "cutoff": "12:00", "cutoff_source": "derived"}))
            for action, body, expected in steps:
                status, _, answer = await _send(server, f"POST /api/v1/morning_brief/{action}",
                                                _bearer(TOKEN), json.dumps(body))
                assert status == 200 and answer["result"] == expected, answer
                api_v1.validate_arguments(api_v1.find("POST", f"/api/v1/morning_brief/{action}").result, answer["result"])
            for body in ({}, {"time": "9:15"}, {"enabled": "yes"}, {"cutoff": "09:15"}, {"cutoff": "24:00"}):
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


@pytest.mark.parametrize("body", [{"cutoff": "9:30"}, {"cutoff": "24:00"}, {"cutoff": 1200},
                                  {"cutoff": "09:15"}, {"cutoff": "08:00"},
                                  {"time": "15:00", "cutoff": "14:00"},
                                  {"time": "23:59", "cutoff": "23:59"}])
def test_invalid_cutoff_or_send_time_is_atomic(settings, body):
    async def check():
        await chat_settings.load()
        with pytest.raises(ValueError):
            await chat_settings.update_morning_brief(body)
        assert settings == ({}, [])
    asyncio.run(check())


def test_cutoff_persists_reloads_and_null_restores_noon(settings):
    async def check():
        await chat_settings.load()
        assert (await chat_settings.update_morning_brief({"cutoff": "11:30"}))["cutoff"] == "11:30"
        assert settings[0]["morning_brief.cutoff"] == "11:30"
        await chat_settings.load()
        assert chat_settings.morning_brief_view()["cutoff"] == "11:30"
        assert chat_settings.morning_brief_view()["cutoff_source"] == "stored"
        assert (await chat_settings.update_morning_brief({"cutoff": None}))["cutoff"] == "12:00"
        assert "morning_brief.cutoff" not in settings[0]
        await chat_settings.update_morning_brief({"time": "15:00", "cutoff": "16:00"})
        assert (await chat_settings.update_morning_brief({"cutoff": None}))["cutoff"] == "18:00"
        assert "morning_brief.cutoff" not in settings[0]
    asyncio.run(check())


@pytest.mark.parametrize(("when", "cutoff"), [
    ("09:15", "12:00"), ("12:00", "15:00"), ("15:00", "18:00"),
    ("20:59", "23:59"), ("21:00", {"kind": "end_of_day"}),
    ("21:30", {"kind": "end_of_day"}), ("23:59", {"kind": "end_of_day"}),
])
def test_missing_cutoff_is_derived_on_load_and_update(settings, when, cutoff):
    async def check():
        settings[0]["morning_brief.time"] = when
        await chat_settings.load()
        assert chat_settings.morning_brief_view() == {
            "enabled": False, "time": when, "cutoff": cutoff, "cutoff_source": "derived"}
        assert "morning_brief.cutoff" not in settings[0]
        await chat_settings.update_morning_brief({"time": "09:15"})
        assert chat_settings.morning_brief_view()["cutoff"] == "12:00"
        await chat_settings.update_morning_brief({"time": when})
        assert chat_settings.morning_brief_view()["cutoff"] == cutoff
    asyncio.run(check())


@pytest.mark.parametrize("cutoff", ["14:00", "15:00"])
def test_legacy_cutoff_not_later_than_send_is_ignored_with_one_warning(settings, caplog, cutoff):
    async def check():
        settings[0].update({"morning_brief.time": "15:00", "morning_brief.cutoff": cutoff})
        await chat_settings.load()
        for _ in range(7):
            assert chat_settings.morning_brief_view() == {
                "enabled": False, "time": "15:00", "cutoff": "18:00", "cutoff_source": "derived"}
        warnings = [record for record in caplog.records if record.name == "looper.agents.chat_settings"]
        assert len(warnings) == 1 and "cutoff" in warnings[0].message
        assert settings[1] == []
    asyncio.run(check())


def test_invalid_cutoff_is_refused_before_confirmation(settings):
    from looper.agents.ops import ErrorCode, OpError

    operation = api_v1.find("POST", "/api/v1/morning_brief/update")

    async def check():
        await chat_settings.load()
        with pytest.raises(OpError) as error:
            await api_v1.domain_for(operation).impact(
                None, operation, {"time": "15:00", "cutoff": "14:00"}, api_v1.OPERATOR, {})
        assert error.value.code == ErrorCode.INVALID
        assert settings == ({}, [])
    asyncio.run(check())
