"""Session tool settings are opt-in and inaccessible to client credentials."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from looper.agents import api_v1, session_tools_settings
from looper.agents.ops.clients import ClientOps
from tests.test_api_v1_auth import CLIENT_TOKEN, _bearer, _send, _serving, clients  # noqa: F401


@pytest.mark.parametrize("enabled", [False, True])
@pytest.mark.parametrize("manager_id", ["manager", "manager-synthetic"])
@pytest.mark.parametrize("key", ["main", "group:channel-lark:synthetic", "dm:channel-lark:synthetic"])
def test_tools_switch_only_changes_the_default_managers_non_main_sessions(monkeypatch, enabled, manager_id, key):
    from looper.agents import session_visibility

    monkeypatch.setattr(session_tools_settings, "_enabled", enabled)
    policy = session_visibility.for_session(key, manager_id=manager_id)
    if manager_id != "manager" or key == "main":
        assert policy is None
    else:
        expected = session_visibility.NON_MAIN_WITH_TOOLS if enabled else session_visibility.NON_MAIN_ALLOWLIST
        assert policy is expected


def test_setting_defaults_persists_loads_and_resets(monkeypatch):
    stored = {}

    async def read(keys):
        return {key: stored[key] for key in keys if key in stored}

    async def write(values):
        for key, value in values.items():
            if value is None:
                stored.pop(key, None)
            else:
                stored[key] = value

    monkeypatch.setattr(session_tools_settings.db_settings, "get_settings", read)
    monkeypatch.setattr(session_tools_settings.db_settings, "put_settings", write)
    monkeypatch.setattr(session_tools_settings, "_enabled", False)

    async def check():
        await session_tools_settings.load()
        assert session_tools_settings.view() == {"enabled": False}
        assert await session_tools_settings.update(True) == {"enabled": True}
        session_tools_settings.set_enabled(False)
        await session_tools_settings.load()
        assert session_tools_settings.enabled()
        assert await session_tools_settings.update(None) == {"enabled": False}
        assert not stored
        for value in ("true", 1, [], {}):
            with pytest.raises(ValueError):
                await session_tools_settings.update(value)
        assert not stored and not session_tools_settings.enabled()
        stored[session_tools_settings.KEY] = "invalid"
        await session_tools_settings.load()
        assert not session_tools_settings.enabled()
        monkeypatch.setattr(session_tools_settings.db_settings, "put_settings", AsyncMock(side_effect=OSError))
        with pytest.raises(OSError):
            await session_tools_settings.update(True)
        assert not session_tools_settings.enabled()

    asyncio.run(check())


@pytest.mark.parametrize("name,arguments", [
    ("session_tools.get", {}), ("session_tools.update", {"enabled": True}),
    ("manager_sessions.assign_task", {"task_id": "task-old", "session": "group:channel-lark:chat-a",
                                       "acknowledge": True}),
])
@pytest.mark.usefixtures("clients")
def test_only_operator_can_use_session_controls(tmp_path, monkeypatch, name, arguments):
    operation = next(op for op in api_v1.operations() if op.operation_id == name)
    assert operation.operator_only and operation.tool is None
    run = AsyncMock(side_effect=AssertionError("client reached an operator control"))
    monkeypatch.setattr(api_v1.domain_for(operation), "run", run)

    async def check():
        async with _serving(tmp_path) as server:
            server._client_ops = ClientOps(SimpleNamespace())
            status, _, answer = await _send(server, f"POST {operation.path}", _bearer(CLIENT_TOKEN),
                                            json.dumps(arguments))
            assert status == 403 and answer["error"]["code"] == "forbidden"
            run.assert_not_awaited()
    asyncio.run(check())
