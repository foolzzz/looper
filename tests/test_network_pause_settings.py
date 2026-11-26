"""Network recovery settings are typed, default off and operator-only."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from looper.agents import api_v1, network_pause_settings as settings
from looper.agents.action_levels import ActionLevel
from looper.agents.ops.clients import ClientOps
from looper.agents.ops.service import MANAGER_TOOLS
from looper.db import system_settings
from tests.test_api_v1_auth import CLIENT_TOKEN, _bearer, _send, _serving, clients  # noqa: F401
from tests.test_operator_auth import TOKEN


@pytest.fixture(autouse=True)
def _clear_maximum_warning(monkeypatch):
    monkeypatch.setattr(settings, "_warned_maximum", None)


@pytest.fixture
def stored(monkeypatch):
    rows, writes = {}, []

    async def read(keys, **kwargs):
        return {key: rows[key] for key in keys if key in rows}

    async def write(values, **kwargs):
        writes.append(values)
        for key, value in values.items():
            if value is None:
                rows.pop(key, None)
            else:
                rows[key] = value

    monkeypatch.setattr(system_settings, "get_settings", read)
    monkeypatch.setattr(system_settings, "put_settings", write)
    return rows, writes


def test_default_off_persisted_and_reset(stored):
    async def check():
        assert await settings.load() == settings.NetworkPauseSettings(False, 7200)
        assert await settings.update({"enabled": True, "max_pause_seconds": 30}) == settings.NetworkPauseSettings(True, 30)
        assert stored[0] == {settings.ENABLED_KEY: "true", settings.MAX_PAUSE_KEY: "30"}
        assert await settings.load() == settings.NetworkPauseSettings(True, 30)
        assert await settings.update({"enabled": None, "max_pause_seconds": None}) == settings.NetworkPauseSettings(False, 7200)
        assert stored[0] == {}
    asyncio.run(check())


@pytest.mark.parametrize("values", [
    {}, {"enabled": "true"}, {"enabled": 1}, {"max_pause_seconds": True}, {"max_pause_seconds": 0},
    {"max_pause_seconds": 86401}, {"max_pause_seconds": 1.5}, {"enabled": True, "unknown": 1},
])
def test_invalid_values_never_write(stored, values):
    async def check():
        with pytest.raises(ValueError):
            await settings.update(values)
        assert stored == ({}, [])
    asyncio.run(check())


def test_invalid_stored_values_fail_closed(stored):
    stored[0].update({settings.ENABLED_KEY: "yes", settings.MAX_PAUSE_KEY: "-3"})
    assert asyncio.run(settings.load()) == settings.NetworkPauseSettings(False, 7200)


def _invalid_maximum_warnings(caplog):
    message = f"Invalid {settings.MAX_PAUSE_KEY}; using the built-in duration"
    return [record for record in caplog.records
            if record.name == "looper.agents.network_pause_settings" and record.message == message]


def test_invalid_stored_maximum_warns_once_per_value(stored, caplog):
    rows = stored[0]

    async def check():
        rows[settings.MAX_PAUSE_KEY] = "nope"
        with caplog.at_level("WARNING", logger="looper.agents.network_pause_settings"):
            assert await settings.load() == settings.NetworkPauseSettings(False, 7200)
            assert await settings.load() == settings.NetworkPauseSettings(False, 7200)
            assert len(_invalid_maximum_warnings(caplog)) == 1
            rows[settings.MAX_PAUSE_KEY] = "0"
            assert await settings.load() == settings.NetworkPauseSettings(False, 7200)
            assert len(_invalid_maximum_warnings(caplog)) == 2
            rows[settings.MAX_PAUSE_KEY] = "30"
            assert await settings.load() == settings.NetworkPauseSettings(False, 30)
            assert len(_invalid_maximum_warnings(caplog)) == 2
            rows[settings.MAX_PAUSE_KEY] = "nope"
            assert await settings.load() == settings.NetworkPauseSettings(False, 7200)
            assert len(_invalid_maximum_warnings(caplog)) == 3
    asyncio.run(check())


def test_unavailable_settings_fail_closed(monkeypatch):
    monkeypatch.setattr(system_settings, "get_settings", AsyncMock(side_effect=RuntimeError("unavailable")))
    assert asyncio.run(settings.load()) == settings.NetworkPauseSettings(False, 7200)
    with pytest.raises(RuntimeError, match='unavailable'):
        asyncio.run(settings.load(strict=True))


def test_task_detail_documents_public_pause_view():
    from looper.agents.v1_domains.task_views import TASK_DETAIL
    schema = TASK_DETAIL["properties"]["network_pause"]
    value = {"id": 1, "state": "paused", "reason": "Model API unavailable",
             "started_at": "2026-11-25T12:00:00Z", "deadline": "2026-11-25T14:00:00Z"}
    assert api_v1.validate_arguments(schema, value) == value
    assert "network_pause" not in TASK_DETAIL["required"]
    assert schema["additionalProperties"] is False


@pytest.mark.parametrize("action", ["get", "update"])
@pytest.mark.usefixtures("clients")
def test_agent_token_cannot_access_settings(tmp_path, monkeypatch, stored, action):
    operation = api_v1.find("POST", f"/api/v1/network_pause/{action}")
    assert operation is not None and operation.operator_only and operation.tool is None
    assert operation.level is ActionLevel.INSIDE
    assert operation.confirms is (action == "update")
    assert all(settings.ENABLED_KEY not in json.dumps(tool.schema) for tool in MANAGER_TOOLS)
    run = AsyncMock()
    monkeypatch.setattr(api_v1.domain_for(operation), "run", run)

    async def check():
        async with _serving(tmp_path) as server:
            server._client_ops = ClientOps(SimpleNamespace())
            status, _, answer = await _send(server, f"POST {operation.path}", _bearer(CLIENT_TOKEN),
                                           json.dumps({"enabled": True} if action == "update" else {}))
            assert status == 403 and answer["error"]["code"] == "forbidden"
            run.assert_not_awaited()
            assert stored == ({}, [])
    asyncio.run(check())


def test_operator_readback_and_partial_updates(tmp_path, monkeypatch, stored):
    from looper.db import manager_actions
    monkeypatch.setattr(manager_actions, "record", AsyncMock(return_value=1))

    async def check():
        async with _serving(tmp_path) as server:
            server._client_ops = ClientOps(SimpleNamespace())
            for action, body, expected in (
                ("get", {}, {"enabled": False, "max_pause_seconds": 7200}),
                ("update", {"max_pause_seconds": 45}, {"enabled": False, "max_pause_seconds": 45}),
                ("update", {"enabled": True}, {"enabled": True, "max_pause_seconds": 45}),
                ("get", {}, {"enabled": True, "max_pause_seconds": 45}),
                ("update", {"enabled": None}, {"enabled": False, "max_pause_seconds": 45}),
            ):
                status, _, answer = await _send(server, f"POST /api/v1/network_pause/{action}",
                                               _bearer(TOKEN), json.dumps(body))
                assert status == 200 and answer["result"] == expected, answer
    asyncio.run(check())
