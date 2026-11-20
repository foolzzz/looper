"""Enabled Lark and Telegram bots with no operator id (assistant layer RFC, section 5.7).

Runs only when LOOPER_TEST_DATABASE_URL points at a disposable database.
"""

from __future__ import annotations

import json

import pytest

from looper.agents.channels.operator_ids import BotRef, missing
from looper.agents.operator_identity import OPERATOR_KEY
from tests.db_support import DSN, run_db

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")


@pytest.mark.parametrize(("channel_type", "enabled", "operator", "listed"), [
    ("lark_bot", True, None, True),          # the key is absent
    ("telegram", True, "", True),             # blank
    ("lark", True, "   ", True),              # blank after the surrounding space is ignored
    ("lark", True, "ou_example", False),      # filled; this does not re-check the id
    ("telegram", False, None, False),         # disabled
    ("web", True, None, False),               # not a Lark or Telegram bot
])
def test_missing_lists_only_enabled_bots_without_an_operator_id(
        monkeypatch: pytest.MonkeyPatch, channel_type: str, enabled: bool, operator: str | None, listed: bool) -> None:
    async def test(pool) -> None:
        name = f"Example {channel_type}"
        channel_id = f"channel-{channel_type}"
        config = {} if operator is None else {OPERATOR_KEY: operator}
        await pool.execute(
            "INSERT INTO channel_configs (channel_id, channel_type, display_name, enabled, config) "
            "VALUES ($1, $2, $3, $4, $5::jsonb)",
            channel_id, channel_type, name, enabled, json.dumps(config))
        found = await missing()
        assert found == ([BotRef(channel_id, channel_type, name)] if listed else [])

    run_db(test, monkeypatch, timeout=30)
