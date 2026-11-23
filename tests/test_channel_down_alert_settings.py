"""Channel outage thresholds are persisted separately from agent-accessible chat settings."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from looper.agents import api_v1, chat_settings
from looper.agents.channels import down_alert_settings
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

    monkeypatch.setattr(down_alert_settings.db_settings, "get_settings", read)
    monkeypatch.setattr(down_alert_settings.db_settings, "put_settings", write)
    monkeypatch.setattr(down_alert_settings, "_minutes", 10)
    return stored, writes


def test_default_persistence_reload_and_null_reset(settings):
    stored, writes = settings

    async def check():
        await down_alert_settings.load()
        assert down_alert_settings.minutes() == 10
        assert await down_alert_settings.update(15) == {"down_alert_minutes": 15}
        assert stored == {"channels.down_alert_minutes": "15"}
        down_alert_settings._minutes = 10
        await down_alert_settings.load()
        assert down_alert_settings.minutes() == 15
        assert await down_alert_settings.update(None) == {"down_alert_minutes": 10}
        assert stored == {}
        stored[down_alert_settings.KEY] = "invalid"
        await down_alert_settings.load()
        assert down_alert_settings.minutes() == 10
        assert len(writes) == 2

    asyncio.run(check())


@pytest.mark.parametrize("value", [0, -1, True, 1.5, "10", [], {}])
def test_invalid_threshold_cannot_write_or_change_memory(settings, value):
    async def check():
        with pytest.raises(ValueError, match="positive whole number"):
            await down_alert_settings.update(value)
        assert settings == ({}, [])
        assert down_alert_settings.minutes() == 10
    asyncio.run(check())


@pytest.mark.parametrize("action", ["get", "update"])
@pytest.mark.usefixtures("clients")
def test_agent_tokens_cannot_access_threshold(tmp_path, monkeypatch, settings, action):
    operation = api_v1.find("POST", f"/api/v1/channel_down_alert/{action}")
    assert operation is not None and operation.operator_only and operation.tool is None
    assert all("down_alert_minutes" not in json.dumps(tool.schema) for tool in MANAGER_TOOLS)
    run = AsyncMock(side_effect=AssertionError("agent token reached the setting"))
    monkeypatch.setattr(api_v1.domain_for(operation), "run", run)

    async def check():
        async with _serving(tmp_path) as server:
            server._client_ops = ClientOps(SimpleNamespace())
            status, _, answer = await _send(server, f"POST {operation.path}", _bearer(CLIENT_TOKEN),
                                            json.dumps({"down_alert_minutes": 15} if action == "update" else {}))
            assert status == 403 and answer["error"]["code"] == "forbidden"
            run.assert_not_awaited()
            assert settings[1] == []
    asyncio.run(check())


def test_operator_reads_updates_and_resets_through_http(tmp_path, monkeypatch, settings):
    from looper.db import manager_actions

    monkeypatch.setattr(manager_actions, "record", AsyncMock(return_value=1))

    async def check():
        async with _serving(tmp_path) as server:
            server._client_ops = ClientOps(SimpleNamespace())
            for action, body, expected in (("get", {}, 10), ("update", {"down_alert_minutes": 15}, 15),
                                           ("get", {}, 15), ("update", {"down_alert_minutes": None}, 10)):
                status, _, answer = await _send(server, f"POST /api/v1/channel_down_alert/{action}",
                                                _bearer(TOKEN), json.dumps(body))
                assert status == 200 and answer["result"] == {"down_alert_minutes": expected}, answer
            for invalid in (0, True, "10"):
                status, _, answer = await _send(server, "POST /api/v1/channel_down_alert/update",
                                                _bearer(TOKEN), json.dumps({"down_alert_minutes": invalid}))
                assert status == 400 and answer["error"]["code"] == "invalid", answer
    asyncio.run(check())


def test_threshold_is_absent_from_chat_settings_and_declares_confirmation(settings):
    assert "down_alert_minutes" not in chat_settings.FIELDS
    with pytest.raises(ValueError, match="unknown setting"):
        chat_settings.checked({"down_alert_minutes": 15})
    operation = api_v1.find("POST", "/api/v1/channel_down_alert/update")
    assert operation.risk == "high" and operation.confirms

    async def check():
        impact = await api_v1.domain_for(operation).impact(
            None, operation, {"down_alert_minutes": None}, api_v1.OPERATOR, {})
        assert "channels.down_alert_minutes to 10" in impact
        assert settings == ({}, [])
    asyncio.run(check())


def test_failed_write_preserves_effective_threshold(monkeypatch, settings):
    monkeypatch.setattr(down_alert_settings.db_settings, "put_settings", AsyncMock(side_effect=RuntimeError("unavailable")))

    async def check():
        with pytest.raises(RuntimeError, match="unavailable"):
            await down_alert_settings.update(15)
        assert down_alert_settings.minutes() == 10
    asyncio.run(check())


def test_database_roundtrip_and_restart_reload(monkeypatch):
    from tests.db_support import run_db

    monkeypatch.setattr(down_alert_settings, "_minutes", 10)

    async def check(pool):
        await down_alert_settings.update(15)
        assert await pool.fetchval("SELECT value FROM system_settings WHERE key = $1", down_alert_settings.KEY) == "15"
        down_alert_settings._minutes = 10
        await down_alert_settings.load()
        assert down_alert_settings.minutes() == 15
        await down_alert_settings.update(None)
        assert await pool.fetchval("SELECT value FROM system_settings WHERE key = $1", down_alert_settings.KEY) is None
        assert down_alert_settings.minutes() == 10

    run_db(check, monkeypatch)
