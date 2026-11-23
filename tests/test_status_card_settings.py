"""The status card's own operator settings. They are not chat settings, and the card stays off until turned on."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from looper.agents import api_v1, chat_settings, status_card
from looper.agents.action_levels import ActionLevel
from looper.agents.ops.clients import ClientOps
from tests.test_api_v1_auth import CLIENT_TOKEN, _bearer, _send, _serving, clients  # noqa: F401
from tests.test_operator_auth import TOKEN

_VIEW = {"stop_enabled": False, "completion_turns": "off", "research_order": False, "ledger_enabled": False}
_IMPACT = (
    "Turning it on makes /status a code snapshot for the operator only: Telegram stops asking the model, "
    "and Lark and console /status stop being ordinary messages. status_card.get starts working. "
    "Turning it off restores the current behavior."
)


@pytest.fixture
def settings(monkeypatch: pytest.MonkeyPatch) -> tuple[dict[str, str], list[dict[str, str | None]]]:
    """A system_settings stand-in, and the chat flags ``load`` writes put back when the test ends."""
    from looper.agents import session_routing

    stored: dict[str, str] = {}
    writes: list[dict[str, str | None]] = []
    for name, value in (
        ("_stop_enabled", False), ("_completion_turns", chat_settings.CompletionTurns.OFF),
        ("_research_order", False), ("_metrics_enabled", False), ("_outbound_enabled", False),
        ("_outbound_limits", {}), ("_delivery_guard_enabled", False), ("_delivery_enabled", False),
        ("_turn_context_enabled", False), ("_timezone", None), ("_reply_language", "zh-CN"),
    ):
        monkeypatch.setattr(chat_settings, name, value)
    monkeypatch.setattr(status_card, "_enabled", False)
    monkeypatch.setattr(session_routing, "_routing_enabled", False)
    monkeypatch.setattr(session_routing, "_max_resident", session_routing.DEFAULT_MAX_RESIDENT)

    async def read(keys, **_: object) -> dict[str, str]:
        return {key: stored[key] for key in keys if key in stored}

    async def write(values, **_: object) -> None:
        writes.append(dict(values))
        for key, value in values.items():
            if value is None:
                stored.pop(key, None)
            else:
                stored[key] = value

    monkeypatch.setattr(chat_settings.db_settings, "get_settings", read)
    monkeypatch.setattr(chat_settings.db_settings, "put_settings", write)
    monkeypatch.setattr(chat_settings.db_ledger, "enabled", AsyncMock(return_value=False))
    return stored, writes


def test_setting_is_default_off_persisted_loaded_and_reset(settings) -> None:
    stored, writes = settings

    async def check() -> None:
        await chat_settings.load()
        assert status_card.enabled() is False and status_card.view() == {"enabled": False}
        assert await status_card.update(True) == {"enabled": True}
        assert stored[status_card.ENABLED_KEY] == "true" and status_card.enabled() is True
        status_card.set_enabled(False)
        await chat_settings.load()
        assert status_card.enabled() is True
        assert await status_card.update(False) == {"enabled": False}
        assert stored[status_card.ENABLED_KEY] == "false"
        assert await status_card.update(None) == {"enabled": False}
        assert status_card.ENABLED_KEY not in stored and status_card.enabled() is False
        stored[status_card.ENABLED_KEY] = "garbage"
        await chat_settings.load()
        assert status_card.enabled() is False
        stored[status_card.ENABLED_KEY] = "TRUE"
        await chat_settings.load()
        assert status_card.enabled() is False
        assert len(writes) == 3

    asyncio.run(check())


@pytest.mark.parametrize("value", [1, "yes", "true", []])
def test_invalid_setting_cannot_write_or_change_memory(settings, value) -> None:
    async def check() -> None:
        with pytest.raises(ValueError, match="true, false or null"):
            await status_card.update(value)
        assert settings == ({}, [])
        assert status_card.enabled() is False

    asyncio.run(check())


def test_the_switch_is_not_a_chat_setting(settings) -> None:
    async def check() -> None:
        await chat_settings.load()
        assert chat_settings.FIELDS == ("stop_enabled", "completion_turns", "research_order", "ledger_enabled")
        assert await chat_settings.settings_view() == _VIEW
        assert await chat_settings.update({"research_order": False}) == _VIEW
        assert settings[1] == [{"manager.research_order": "false"}]
        with pytest.raises(ValueError, match="unknown setting"):
            chat_settings.checked({"status_card_enabled": True})
        assert status_card.ENABLED_KEY not in settings[0]

    asyncio.run(check())


_PATHS = {
    "status_card_settings.get": "/api/v1/status_card_settings/get",
    "status_card_settings.update": "/api/v1/status_card_settings/update",
    "status_card.get": "/api/v1/status_card/get",
}


@pytest.mark.parametrize("operation_id,risk,confirms", [
    ("status_card_settings.get", "read", False),
    ("status_card_settings.update", "high", True),
    ("status_card.get", "read", False),
])
def test_operations_are_inside_and_operator_only(operation_id: str, risk: str, confirms: bool) -> None:
    operation = api_v1.find("POST", _PATHS[operation_id])
    assert operation is not None
    assert operation.operation_id == operation_id
    assert operation.operator_only and operation.tool is None
    assert operation.level is ActionLevel.INSIDE
    assert operation.risk == risk and operation.confirms is confirms
    assert 0 < len(operation.summary) <= 120
    assert operation.description.startswith(operation.summary)


@pytest.mark.parametrize("value,enabled,default", [(True, "true", ""), (False, "false", ""),
                                                   (None, "false", " (built-in default)")])
def test_update_declares_high_risk_confirmation(settings, value, enabled: str, default: str) -> None:
    operation = api_v1.find("POST", "/api/v1/status_card_settings/update")

    async def check() -> None:
        impact = await api_v1.domain_for(operation).impact(None, operation, {"enabled": value}, api_v1.OPERATOR, {})
        assert impact == f"Set status_card.enabled to {enabled}{default}? " + _IMPACT
        assert settings == ({}, [])

    asyncio.run(check())


@pytest.mark.parametrize("action,body", [("get", {}), ("update", {"enabled": True})])
@pytest.mark.usefixtures("clients")
def test_agent_token_cannot_read_or_change_the_card(tmp_path, monkeypatch, settings, action: str, body: dict) -> None:
    operation = api_v1.find("POST", f"/api/v1/status_card_settings/{action}")
    run = AsyncMock(side_effect=AssertionError("agent token reached the status card"))
    monkeypatch.setattr(api_v1.domain_for(operation), "run", run)

    async def check() -> None:
        async with _serving(tmp_path) as server:
            server._client_ops = ClientOps(SimpleNamespace())
            status, _, answer = await _send(server, f"POST {operation.path}", _bearer(CLIENT_TOKEN), json.dumps(body))
            assert status == 403 and answer["error"]["code"] == "forbidden"
            run.assert_not_awaited()
            assert settings[1] == []
            assert status_card.enabled() is False

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
                status, _, answer = await _send(server, f"POST /api/v1/status_card_settings/{action}",
                                                _bearer(TOKEN), json.dumps(body))
                assert status == 200 and answer["result"] == {"enabled": expected}, answer
            assert status_card.enabled() is False

    asyncio.run(check())


@pytest.mark.usefixtures("clients")
def test_a_bad_update_leaves_the_switch_off(tmp_path, monkeypatch, settings) -> None:
    """A high-risk call validates before it writes. A non-boolean ``enabled`` is an error and changes nothing."""
    from looper.db import manager_actions

    monkeypatch.setattr(manager_actions, "record", AsyncMock(return_value=1))

    async def check() -> None:
        async with _serving(tmp_path) as server:
            server._client_ops = ClientOps(SimpleNamespace())
            status, _, answer = await _send(server, "POST /api/v1/status_card_settings/update", _bearer(TOKEN),
                                            json.dumps({"enabled": "yes"}))
            assert answer["error"]["code"] in ("invalid", "unavailable")
            assert status != 200
            assert settings == ({}, [])
            assert status_card.enabled() is False

    asyncio.run(check())


def test_get_conflicts_while_off_and_returns_the_snapshot_while_on(tmp_path, monkeypatch, settings) -> None:
    from looper.db import manager_actions

    monkeypatch.setattr(manager_actions, "record", AsyncMock(return_value=1))

    async def check() -> None:
        await chat_settings.load()
        async with _serving(tmp_path) as server:
            server._client_ops = ClientOps(SimpleNamespace())
            status, _, answer = await _send(server, "POST /api/v1/status_card/get", _bearer(TOKEN), "{}")
            assert status == 409 and answer["error"]["code"] == "conflict"
            status, _, answer = await _send(server, "POST /api/v1/status_card_settings/update", _bearer(TOKEN),
                                            json.dumps({"enabled": True}))
            assert status == 200 and answer["result"] == {"enabled": True}
            status, _, answer = await _send(server, "POST /api/v1/status_card/get", _bearer(TOKEN), "{}")
            assert status == 200, answer
            card = answer["result"]
            assert card["text"].startswith("截至 ")
            assert "recent" not in card and "lanes" not in json.dumps(card)
            assert {section["name"] for section in card["sections"]} >= {"manager", "tasks", "schedules"}
            assert "waiting" not in {section["name"] for section in card["sections"]}
            assert "reminders" not in {section["name"] for section in card["sections"]}

    asyncio.run(check())
