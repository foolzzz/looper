"""Work mode is a durable, default-off operator setting."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from looper.agents import api_v1, chat_settings, task_conversations_settings as settings
from looper.agents.action_levels import ActionLevel
from looper.agents.ops.clients import ClientOps
from tests.db_support import run_db
from tests.test_api_v1_auth import CLIENT_TOKEN, _bearer, _send, _serving, clients  # noqa: F401
from tests.test_operator_auth import TOKEN


@pytest.fixture(autouse=True)
def reset_settings(monkeypatch):
    monkeypatch.setattr(settings, "_enabled", False)


def test_switch_default_off(monkeypatch, tmp_path):
    async def check(pool):
        await settings.load()
        assert settings.enabled() is False
        async with _serving(tmp_path) as server:
            server._client_ops = ClientOps(SimpleNamespace())
            status, _, body = await _send(server, "POST /api/v1/task_conversations/get", _bearer(TOKEN), "{}")
            assert status == 200 and body["result"] == {"enabled": False}
            assert await settings.update(True) == {"enabled": True}
            assert await pool.fetchval("SELECT value FROM system_settings WHERE key=$1", settings.KEY) == "true"
            settings.set_enabled(False)
            await settings.load()
            assert settings.enabled() is True
            assert await settings.update(None) == {"enabled": False}
            assert await pool.fetchval("SELECT value FROM system_settings WHERE key=$1", settings.KEY) is None
            status, _, body = await _send(server, "POST /api/v1/task_conversations/update", _bearer(TOKEN),
                                          json.dumps({"enabled": "true"}))
            assert status == 400 and body["error"]["code"] == "invalid"
    run_db(check, monkeypatch)


@pytest.mark.parametrize("value", ["true", "false", 0, 1, [], {}])
def test_invalid_setting_never_writes(monkeypatch, value):
    write = AsyncMock()
    monkeypatch.setattr(settings.db_settings, "put_settings", write)
    async def check():
        with pytest.raises(ValueError, match="true, false or null"):
            await settings.update(value)
        write.assert_not_awaited()
        assert settings.enabled() is False
    asyncio.run(check())


def test_failed_setting_write_keeps_memory(monkeypatch):
    monkeypatch.setattr(settings.db_settings, "put_settings", AsyncMock(side_effect=RuntimeError("offline")))
    async def check():
        with pytest.raises(RuntimeError, match="offline"):
            await settings.update(True)
        assert settings.enabled() is False
    asyncio.run(check())


@pytest.mark.parametrize("action", ["get", "update"])
@pytest.mark.usefixtures("clients")
def test_work_mode_settings_are_operator_only(monkeypatch, tmp_path, action):
    op = api_v1.find("POST", f"/api/v1/task_conversations/{action}")
    assert op.operator_only and op.tool is None and op.level is ActionLevel.INSIDE
    assert op.risk == ("read" if action == "get" else "high")
    assert op.confirms is (action == "update")
    reached = AsyncMock()
    monkeypatch.setattr(api_v1.domain_for(op), "run", reached)
    async def check():
        async with _serving(tmp_path) as server:
            server._client_ops = ClientOps(SimpleNamespace())
            status, _, body = await _send(server, f"POST {op.path}", _bearer(CLIENT_TOKEN),
                                          json.dumps({"enabled": True} if action == "update" else {}))
            assert status == 403 and body["error"]["code"] == "forbidden"
            reached.assert_not_awaited()
    asyncio.run(check())


def test_confirmation_and_chat_settings_boundary():
    for name in ("tasks.conversations.enabled", "task_conversations_enabled"):
        with pytest.raises(ValueError, match="unknown setting"):
            chat_settings.checked({name: True})
    op = api_v1.find("POST", "/api/v1/task_conversations/update")
    async def check():
        text = await api_v1.domain_for(op).impact(None, op, {"enabled": True}, api_v1.OPERATOR, {})
        assert text == ("Set tasks.conversations.enabled to true? On, the dashboard can create draft tasks; "
                        "nothing else changes yet.")
    asyncio.run(check())
