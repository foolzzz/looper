"""The chat's settings on a real PostgreSQL: off with no row, stored through the API, read back at startup.

Runs only when LOOPER_TEST_DATABASE_URL points at a disposable database.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from looper.agents import chat_settings
from tests.db_support import DSN
from tests.db_support import run_db
from tests.test_api_v1_chat_session_controls_db import _api

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")


def test_the_stop_setting_is_stored_and_loaded(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    async def test(pool: Any) -> None:
        async with _api(tmp_path, pool, None) as (call, _):
            assert await chat_settings.load() is False
            assert (await call("chat_settings.get"))[1]["result"] == {"stop_enabled": False, "completion_turns": "off", "research_order": False, "outbound_enabled": False}
            assert (await call("chat_settings.update", {"stop_enabled": True}))[1]["result"] == {
                "stop_enabled": True, "completion_turns": "off", "research_order": False, "outbound_enabled": False}
            chat_settings.set_stop_enabled(False)  # a restart
            assert await chat_settings.load() is True
            assert (await call("chat_settings.update", {"stop_enabled": None}))[1]["result"] == {
                "stop_enabled": False, "completion_turns": "off", "research_order": False, "outbound_enabled": False}
            assert await chat_settings.load() is False

    run_db(test, monkeypatch, timeout=30)


def test_the_completion_turns_setting_is_stored_alone_and_loaded(monkeypatch: pytest.MonkeyPatch,
                                                                tmp_path: Path) -> None:
    async def test(pool: Any) -> None:
        async with _api(tmp_path, pool, None) as (call, _):
            await chat_settings.load()
            assert chat_settings.completion_turns() is chat_settings.CompletionTurns.OFF
            await call("chat_settings.update", {"stop_enabled": True})
            status, answer = await call("chat_settings.update", {"completion_turns": "dm_only", "research_order": False})
            assert status == 200 and answer["result"] == {"stop_enabled": True, "completion_turns": "dm_only", "research_order": False, "outbound_enabled": False}
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


def test_outbound_is_operator_controlled_and_survives_restart(monkeypatch: pytest.MonkeyPatch,
                                                            tmp_path: Path) -> None:
    async def test(pool: Any) -> None:
        monkeypatch.setattr(chat_settings, "_outbound_enabled", False)
        async with _api(tmp_path, pool, None) as (call, _):
            await chat_settings.load()
            assert chat_settings.outbound_enabled() is False
            for operation in ("outbound_settings.update", "chat_settings.update"):
                status, answer = await call(operation, {"outbound_enabled": True}, client=True)
                assert status == 403 and answer["error"]["code"] == "forbidden"
            status, answer = await call("outbound_settings.update", {"outbound_enabled": True})
            assert status == 200 and answer["result"] == {"outbound_enabled": True, **chat_settings.OUTBOUND_DEFAULTS}
            assert await pool.fetchval("SELECT value FROM system_settings WHERE key=$1",
                                       chat_settings.OUTBOUND_ENABLED_KEY) == "true"
            chat_settings.set_outbound_enabled(False)
            await chat_settings.load()
            assert chat_settings.outbound_enabled() is True
            status, answer = await call("outbound_settings.update", {"outbound_enabled": None})
            assert status == 200 and answer["result"] == {"outbound_enabled": False, **chat_settings.OUTBOUND_DEFAULTS}
            assert await pool.fetchval("SELECT count(*) FROM system_settings WHERE key=$1",
                                       chat_settings.OUTBOUND_ENABLED_KEY) == 0

    run_db(test, monkeypatch, timeout=30)
