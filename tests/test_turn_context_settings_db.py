"""Operator turn-context settings roundtrip on disposable PostgreSQL."""
import pytest

from looper.agents import chat_settings
from tests.db_support import DSN, run_db, v1_api

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")


def test_turn_context_settings_roundtrip_and_missing_operator_notice(monkeypatch, tmp_path):
    from looper.agents.channels import operator_ids
    from unittest.mock import AsyncMock

    monkeypatch.setattr(operator_ids, "missing", AsyncMock(return_value=[
        operator_ids.BotRef("bot-example", "lark", "Example bot")]))
    async def test(pool):
        async with v1_api(tmp_path, pool) as call:
            await chat_settings.load()
            status, answer = await call("turn_context_settings.update", {
                "turn_context_enabled": True, "timezone": "Asia/Shanghai", "reply_language": "en"})
            assert status == 200
            assert answer["result"]["missing_operator_ids"] == [
                {"channel_id": "bot-example", "channel_type": "lark", "name": "Example bot"}]
            assert await pool.fetchval("SELECT value FROM system_settings WHERE key='user.timezone'") == "Asia/Shanghai"
            await chat_settings.load()
            result = (await call("turn_context_settings.get"))[1]["result"]
            assert result["turn_context_enabled"] and result["timezone"] == "Asia/Shanghai" and result["reply_language"] == "en"
            assert (await call("turn_context_settings.update", {"timezone": "Invalid/Zone"}))[0] == 400
            assert chat_settings.timezone() == "Asia/Shanghai"
            await call("turn_context_settings.update", {"turn_context_enabled": None, "timezone": None, "reply_language": None})
            assert await pool.fetchval("SELECT count(*) FROM system_settings WHERE key=ANY($1)",
                                       [chat_settings.TURN_CONTEXT_KEY, chat_settings.TIMEZONE_KEY, chat_settings.REPLY_LANGUAGE_KEY]) == 0
    run_db(test, monkeypatch, timeout=30)


@pytest.mark.parametrize("field,value", [
    ("turn_context_enabled", True), ("timezone", "Asia/Shanghai"), ("reply_language", "en"),
])
def test_a_client_cannot_write_new_context_fields(monkeypatch, tmp_path, field, value):
    async def test(pool):
        async with v1_api(tmp_path, pool) as call:
            status, answer = await call("turn_context_settings.update", {field: value}, client=True)
            assert status == 403 and answer["error"]["code"] == "forbidden"
            assert await pool.fetchval("SELECT count(*) FROM system_settings") == 0
            assert (await call("turn_context_settings.get", client=True))[0] == 403
            status, answer = await call("turn_context_settings.update", {field: value})
            assert status == 200 and answer["result"][field] == value
    run_db(test, monkeypatch, timeout=30)
