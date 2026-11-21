"""Settings → Chat turns the ledger on and off (assistant layer RFC, section 5.7).

Runs only when LOOPER_TEST_DATABASE_URL points at a disposable database.
"""

from __future__ import annotations

import json

import pytest

from looper.agents import api_v1, chat_settings
from looper.agents.ops import OpError
from looper.agents.v1_domains import chat_controls
from looper.db import ledger
from tests.db_support import DSN, run_db

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")


async def _call(name: str, body: dict | None = None):
    operation = api_v1.find("POST", "/api/v1/" + name.replace(".", "/"))
    try:
        result = await chat_controls.run(None, operation, body or {}, api_v1.OPERATOR, {})
    except OpError as exc:
        return exc
    api_v1.validate_arguments(operation.result, result)
    return result


def test_turning_the_ledger_on_stores_true_and_returns_the_bots(monkeypatch: pytest.MonkeyPatch) -> None:
    async def test(pool) -> None:
        await pool.execute(
            "INSERT INTO channel_configs (channel_id, channel_type, display_name, enabled, config) VALUES "
            "('channel-lark', 'lark', 'Example Lark', true, '{}'), "
            "('channel-telegram', 'telegram', 'Example Telegram', true, $1::jsonb)",
            json.dumps({"operator_sender_id": "12345"}))
        result = await _call("chat_settings.update", {"ledger_enabled": True})
        assert result["ledger_enabled"] is True
        assert result["missing_operator_ids"] == [
            {"channel_id": "channel-lark", "channel_type": "lark", "name": "Example Lark"}]
        assert await pool.fetchval("SELECT value FROM system_settings WHERE key = $1", ledger.ENABLED_KEY) == "true"
        operation = api_v1.find("POST", "/api/v1/chat_settings/update")
        impact = await chat_controls.impact(None, operation, {"ledger_enabled": True}, api_v1.OPERATOR, {})
        assert "Example Lark" in impact and "Example Telegram" not in impact
        again = await _call("chat_settings.update", {"stop_enabled": True})
        assert "missing_operator_ids" not in again and again["ledger_enabled"] is True
        await pool.execute("UPDATE channel_configs SET enabled = false WHERE channel_id = 'channel-lark'")
        empty = await _call("chat_settings.update", {"ledger_enabled": True})
        assert empty["missing_operator_ids"] == []

    run_db(test, monkeypatch, timeout=30)


def test_turning_the_ledger_off_stores_false_and_null_restores_the_default(monkeypatch: pytest.MonkeyPatch) -> None:
    async def test(pool) -> None:
        assert (await _call("chat_settings.update", {"ledger_enabled": True}))["ledger_enabled"] is True
        off = await _call("chat_settings.update", {"ledger_enabled": False})
        assert off["ledger_enabled"] is False and "missing_operator_ids" not in off
        assert await pool.fetchval("SELECT value FROM system_settings WHERE key = $1", ledger.ENABLED_KEY) == "false"
        restored = await _call("chat_settings.update", {"ledger_enabled": None})
        assert restored["ledger_enabled"] is False and "missing_operator_ids" not in restored
        assert await pool.fetchval("SELECT count(*) FROM system_settings WHERE key = $1", ledger.ENABLED_KEY) == 0

    run_db(test, monkeypatch, timeout=30)


def test_the_chat_view_reads_the_stored_ledger_value(monkeypatch: pytest.MonkeyPatch) -> None:
    async def test(pool) -> None:
        assert (await _call("chat_settings.get"))["ledger_enabled"] is False
        await pool.execute("INSERT INTO system_settings (key, value) VALUES ($1, 'true')", ledger.ENABLED_KEY)
        assert (await _call("chat_settings.get"))["ledger_enabled"] is True
        await pool.execute("UPDATE system_settings SET value = 'false' WHERE key = $1", ledger.ENABLED_KEY)
        view = await _call("chat_settings.get")
        assert view["ledger_enabled"] is False and "missing_operator_ids" not in view

    run_db(test, monkeypatch, timeout=30)


def test_get_reports_the_ledger_off_and_writes_nothing_when_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    """Off: reading the chat settings does not create ``ledger.enabled``."""

    async def test(pool) -> None:
        before = [tuple(row) for row in await pool.fetch("SELECT key, value FROM system_settings ORDER BY key")]
        result = await _call("chat_settings.get")
        assert result["ledger_enabled"] is False and "missing_operator_ids" not in result
        after = [tuple(row) for row in await pool.fetch("SELECT key, value FROM system_settings ORDER BY key")]
        assert after == before
        assert await pool.fetchval("SELECT count(*) FROM system_settings WHERE key = $1", ledger.ENABLED_KEY) == 0
        refused = await _call("chat_settings.update", {"ledger_enabled": "yes"})
        assert isinstance(refused, OpError) and refused.code.value == "invalid"
        assert await pool.fetchval("SELECT count(*) FROM system_settings WHERE key = $1", ledger.ENABLED_KEY) == 0

    run_db(test, monkeypatch, timeout=30)


def test_checked_refuses_a_ledger_value_that_is_not_a_boolean_or_null() -> None:
    with pytest.raises(ValueError, match="ledger_enabled must be true, false or null"):
        chat_settings.checked({"ledger_enabled": "yes"})
