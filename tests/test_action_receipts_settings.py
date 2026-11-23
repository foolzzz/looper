"""The action receipts switch: default off, operator-only operations, not a chat setting, not reachable by a token."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from looper.agents import api_v1, chat_settings, receipts_settings
from looper.agents.action_levels import ActionLevel
from looper.agents.ops.clients import ClientOps
from tests.test_api_v1_auth import CLIENT_TOKEN, _bearer, _send, _serving, clients  # noqa: F401
from tests.test_operator_auth import TOKEN
from tests.test_status_card_settings import settings  # noqa: F401  (a system_settings stand-in)

KEY = "manager.action_receipts.enabled"


@pytest.fixture(autouse=True)
def _off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(receipts_settings, "_enabled", False)


def test_setting_is_default_off_persisted_loaded_and_reset(settings) -> None:
    stored, writes = settings

    async def check() -> None:
        await chat_settings.load()
        assert receipts_settings.ENABLED_KEY == KEY
        assert receipts_settings.enabled() is False and receipts_settings.view() == {"enabled": False}
        assert await receipts_settings.update(True) == {"enabled": True}
        assert stored[KEY] == "true" and receipts_settings.enabled() is True
        receipts_settings.set_enabled(False)
        await chat_settings.load()
        assert receipts_settings.enabled() is True
        assert await receipts_settings.update(False) == {"enabled": False} and stored[KEY] == "false"
        assert await receipts_settings.update(None) == {"enabled": False}
        assert KEY not in stored and receipts_settings.enabled() is False
        for value in ("garbage", "TRUE"):
            stored[KEY] = value
            await chat_settings.load()
            assert receipts_settings.enabled() is False
        assert len(writes) == 3

    asyncio.run(check())


@pytest.mark.parametrize("value", [1, "yes", "true", []])
def test_invalid_setting_cannot_write_or_change_memory(settings, value) -> None:
    async def check() -> None:
        with pytest.raises(ValueError, match="true, false or null"):
            await receipts_settings.update(value)
        assert settings == ({}, []) and receipts_settings.enabled() is False

    asyncio.run(check())


def test_the_switch_is_not_a_chat_setting(settings) -> None:
    async def check() -> None:
        await chat_settings.load()
        assert chat_settings.FIELDS == ("stop_enabled", "completion_turns", "research_order", "ledger_enabled")
        with pytest.raises(ValueError, match="unknown setting"):
            chat_settings.checked({"action_receipts_enabled": True})
        assert KEY not in settings[0]

    asyncio.run(check())


_PATHS = {"action_receipts.get": "/api/v1/action_receipts/get", "action_receipts.update": "/api/v1/action_receipts/update"}


@pytest.mark.parametrize("operation_id,risk,confirms", [("action_receipts.get", "read", False),
                                                        ("action_receipts.update", "high", True)])
def test_operations_are_inside_and_operator_only(operation_id: str, risk: str, confirms: bool) -> None:
    operation = api_v1.find("POST", _PATHS[operation_id])
    assert operation is not None and operation.operation_id == operation_id
    assert operation.operator_only and operation.tool is None
    assert operation.level is ActionLevel.INSIDE
    assert operation.risk == risk and operation.confirms is confirms
    assert 0 < len(operation.summary) <= 120 and operation.description.startswith(operation.summary)


@pytest.mark.parametrize("value,enabled,default", [(True, "true", ""), (False, "false", ""),
                                                   (None, "false", " (built-in default)")])
def test_update_declares_what_it_changes_and_writes_nothing(settings, value, enabled: str, default: str) -> None:
    operation = api_v1.find("POST", "/api/v1/action_receipts/update")

    async def check() -> None:
        impact = await api_v1.domain_for(operation).impact(None, operation, {"enabled": value}, api_v1.OPERATOR, {})
        assert impact.startswith(f"Set {KEY} to {enabled}{default}? ")
        assert "shown_to_user" in impact and "Off restores the current behavior." in impact
        assert settings == ({}, [])

    asyncio.run(check())


@pytest.mark.parametrize("action,body", [("get", {}), ("update", {"enabled": True})])
@pytest.mark.usefixtures("clients")
def test_agent_token_cannot_read_or_change_the_switch(tmp_path, monkeypatch, settings, action: str, body: dict) -> None:
    operation = api_v1.find("POST", f"/api/v1/action_receipts/{action}")
    run = AsyncMock(side_effect=AssertionError("agent token reached the receipts switch"))
    monkeypatch.setattr(api_v1.domain_for(operation), "run", run)

    async def check() -> None:
        async with _serving(tmp_path) as server:
            server._client_ops = ClientOps(SimpleNamespace())
            status, _, answer = await _send(server, f"POST {operation.path}", _bearer(CLIENT_TOKEN), json.dumps(body))
            assert status == 403 and answer["error"]["code"] == "forbidden"
            run.assert_not_awaited()
            assert settings[1] == [] and receipts_settings.enabled() is False

    asyncio.run(check())


def test_operator_reads_and_updates_through_v1(tmp_path, monkeypatch, settings) -> None:
    from looper.db import manager_actions

    monkeypatch.setattr(manager_actions, "record", AsyncMock(return_value=1))

    async def check() -> None:
        await chat_settings.load()
        async with _serving(tmp_path) as server:
            server._client_ops = ClientOps(SimpleNamespace())
            for action, body, expected in (("get", {}, False), ("update", {"enabled": True}, True),
                                           ("get", {}, True), ("update", {"enabled": None}, False)):
                status, _, answer = await _send(server, f"POST /api/v1/action_receipts/{action}", _bearer(TOKEN),
                                                json.dumps(body))
                assert status == 200 and answer["result"] == {"enabled": expected}, answer
            assert receipts_settings.enabled() is False

    asyncio.run(check())
