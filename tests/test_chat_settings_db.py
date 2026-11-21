"""The chat's settings on a real PostgreSQL: off with no row, stored through the API, read back at startup.

Runs only when LOOPER_TEST_DATABASE_URL points at a disposable database.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from looper.agents import chat_settings
from looper.db import ledger
from tests.db_support import DSN
from tests.db_support import run_db
from tests.test_api_v1_chat_session_controls_db import _api

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")


def test_the_stop_setting_is_stored_and_loaded(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    async def test(pool: Any) -> None:
        async with _api(tmp_path, pool, None) as (call, _):
            assert await chat_settings.load() is False
            assert (await call("chat_settings.get"))[1]["result"] == {"stop_enabled": False, "completion_turns": "off", "research_order": False, "ledger_enabled": False, **{key: (await chat_settings.settings_view())[key] for key in ("turn_context_enabled", "timezone", "server_timezone", "reply_language")}}
            assert (await call("chat_settings.update", {"stop_enabled": True}))[1]["result"] == {
                "stop_enabled": True, "completion_turns": "off", "research_order": False, "ledger_enabled": False, **{key: (await chat_settings.settings_view())[key] for key in ("turn_context_enabled", "timezone", "server_timezone", "reply_language")}}
            chat_settings.set_stop_enabled(False)  # a restart
            assert await chat_settings.load() is True
            assert (await call("chat_settings.update", {"stop_enabled": None}))[1]["result"] == {
                "stop_enabled": False, "completion_turns": "off", "research_order": False, "ledger_enabled": False, **{key: (await chat_settings.settings_view())[key] for key in ("turn_context_enabled", "timezone", "server_timezone", "reply_language")}}
            assert await chat_settings.load() is False

    run_db(test, monkeypatch, timeout=30)


def test_the_completion_turns_setting_is_stored_alone_and_loaded(monkeypatch: pytest.MonkeyPatch,
                                                                tmp_path: Path) -> None:
    async def test(pool: Any) -> None:
        async with _api(tmp_path, pool, None) as (call, _):
            await chat_settings.load()
            assert chat_settings.completion_turns() is chat_settings.CompletionTurns.OFF
            await call("chat_settings.update", {"stop_enabled": True})
            status, answer = await call("chat_settings.update", {"completion_turns": "dm_only", "research_order": False, "ledger_enabled": False})
            assert status == 200 and answer["result"] == {"stop_enabled": True, "completion_turns": "dm_only", "research_order": False, "ledger_enabled": False, **{key: (await chat_settings.settings_view())[key] for key in ("turn_context_enabled", "timezone", "server_timezone", "reply_language")}}
            chat_settings.set_completion_turns(chat_settings.CompletionTurns.OFF)  # a restart
            await chat_settings.load()
            assert chat_settings.completion_turns() is chat_settings.CompletionTurns.DM_ONLY
            assert chat_settings.stop_enabled() is True  # the other setting kept its value
            for bad in ({"completion_turns": "groups"}, {}):
                status, answer = await call("chat_settings.update", bad)
                assert status == 400 and answer["error"]["code"] == "invalid"
            assert chat_settings.completion_turns() is chat_settings.CompletionTurns.DM_ONLY
            status, answer = await call("chat_settings.update", {"completion_turns": None})
            assert answer["result"]["completion_turns"] == "off"
            assert await pool.fetchval("SELECT count(*) FROM system_settings WHERE key = $1",
                                       chat_settings.COMPLETION_TURNS_KEY) == 0
            await pool.execute("INSERT INTO system_settings (key, value) VALUES ($1, 'garbage')",
                               chat_settings.COMPLETION_TURNS_KEY)
            await chat_settings.load()  # an unreadable value is the default
            assert chat_settings.completion_turns() is chat_settings.CompletionTurns.OFF

    run_db(test, monkeypatch, timeout=30)


def test_the_research_order_setting_is_stored_alone_and_loaded(monkeypatch: pytest.MonkeyPatch,
                                                               tmp_path: Path) -> None:
    async def test(pool: Any) -> None:
        async with _api(tmp_path, pool, None) as (call, _):
            await chat_settings.load()
            assert chat_settings.research_order() is False
            status, answer = await call("chat_settings.update", {"research_order": True})
            assert status == 200 and answer["result"]["research_order"] is True
            chat_settings.set_research_order(False)  # a restart
            await chat_settings.load()
            assert chat_settings.research_order() is True and chat_settings.stop_enabled() is False
            status, answer = await call("chat_settings.update", {"research_order": "yes"})
            assert status == 400 and answer["error"]["code"] == "invalid"
            status, answer = await call("chat_settings.update", {"research_order": None})
            assert answer["result"]["research_order"] is False
            assert await pool.fetchval("SELECT count(*) FROM system_settings WHERE key = $1",
                                       chat_settings.RESEARCH_ORDER_KEY) == 0

    run_db(test, monkeypatch, timeout=30)


def test_a_client_cannot_set_ledger_enabled(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """``ledger_enabled`` is operator-only. The rest of the body is not applied."""

    async def test(pool: Any) -> None:
        async with _api(tmp_path, pool, None) as (call, _):
            status, answer = await call("chat_settings.update",
                                        {"ledger_enabled": True, "stop_enabled": True}, client=True)
            assert status == 403 and answer["error"]["code"] == "forbidden"
            assert await pool.fetchval("SELECT count(*) FROM system_settings WHERE key = $1",
                                       ledger.ENABLED_KEY) == 0
            assert await pool.fetchval("SELECT count(*) FROM system_settings WHERE key = $1",
                                       chat_settings.STOP_ENABLED_KEY) == 0
            status, answer = await call("chat_settings.update", {"ledger_enabled": True})
            assert status == 200 and answer["result"]["ledger_enabled"] is True
            assert await pool.fetchval("SELECT value FROM system_settings WHERE key = $1",
                                       ledger.ENABLED_KEY) == "true"

    run_db(test, monkeypatch, timeout=30)


def test_turn_context_settings_roundtrip_and_missing_operator_notice(monkeypatch, tmp_path):
    from looper.agents.channels import operator_ids
    from unittest.mock import AsyncMock

    monkeypatch.setattr(operator_ids, "missing", AsyncMock(return_value=[
        operator_ids.BotRef("bot-example", "lark", "Example bot")]))
    async def test(pool):
        async with _api(tmp_path, pool, None) as (call, _):
            await chat_settings.load()
            status, answer = await call("chat_settings.update", {
                "turn_context_enabled": True, "timezone": "Asia/Shanghai", "reply_language": "en"})
            assert status == 200
            assert answer["result"]["missing_operator_ids"] == [
                {"channel_id": "bot-example", "channel_type": "lark", "name": "Example bot"}]
            assert await pool.fetchval("SELECT value FROM system_settings WHERE key='user.timezone'") == "Asia/Shanghai"
            await chat_settings.load()
            result = (await call("chat_settings.get"))[1]["result"]
            assert result["turn_context_enabled"] and result["timezone"] == "Asia/Shanghai" and result["reply_language"] == "en"
            assert (await call("chat_settings.update", {"timezone": "Invalid/Zone"}))[0] == 400
            assert chat_settings.timezone() == "Asia/Shanghai"
            await call("chat_settings.update", {"turn_context_enabled": None, "timezone": None, "reply_language": None})
            assert await pool.fetchval("SELECT count(*) FROM system_settings WHERE key=ANY($1)",
                                       [chat_settings.TURN_CONTEXT_KEY, chat_settings.TIMEZONE_KEY, chat_settings.REPLY_LANGUAGE_KEY]) == 0
    run_db(test, monkeypatch, timeout=30)


@pytest.mark.parametrize("field,value", [
    ("turn_context_enabled", True), ("timezone", "Asia/Shanghai"), ("reply_language", "en"),
])
def test_a_client_cannot_write_new_context_fields(monkeypatch, tmp_path, field, value):
    async def test(pool):
        async with _api(tmp_path, pool, None) as (call, _):
            status, answer = await call("chat_settings.update", {field: value, "stop_enabled": True}, client=True)
            assert status == 403 and answer["error"]["code"] == "forbidden"
            assert await pool.fetchval("SELECT count(*) FROM system_settings") == 0
            status, answer = await call("chat_settings.update", {field: value})
            assert status == 200 and answer["result"][field] == value
    run_db(test, monkeypatch, timeout=30)
