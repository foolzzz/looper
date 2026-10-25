"""The channel configuration API and its sender allowlists against a real PostgreSQL.

Runs only when LOOPER_TEST_DATABASE_URL points at a disposable database. Each
test works in a new schema of its own, dropped afterwards, seeded the way
``looper serve`` seeds it.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from tests.db_support import DSN, run_with_channel_configs

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")

OPEN_ID = "ou_7dab8a3d3cdcc9da365777c7ad535d62"


async def _call(method: str, path: str, body: Any = None) -> tuple[int, Any]:
    from tests.test_lark_channel_db import _call as call

    return await call(method, path, body)


def test_the_seeded_channels_have_empty_allowlists_and_say_why_they_do_not_start(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _test(pool: Any) -> None:
        status, body = await _call("GET", "channel_configs")
        assert status == 200
        channels = {c["channel_id"]: c for c in body["configs"]}
        assert channels["channel-telegram"]["config"]["allowed_user_ids"] == []
        assert channels["channel-lark"]["config"]["allowed_open_ids"] == []
        for channel in channels.values():
            assert channel["enabled"] is False
            assert "allowlist is empty" in channel["start_problem"]

    run_with_channel_configs(_test, monkeypatch)


@pytest.mark.usefixtures("lark_cli_stand_in")
def test_an_allowlist_round_trips_validated(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _test(pool: Any) -> None:
        status, lark = await _call("PUT", "channel-lark", {
            "enabled": True,
            "config": {"profile": "", "allowed_open_ids": [f" {OPEN_ID} ", OPEN_ID, ""]},
        })
        assert status == 200
        assert lark["enabled"] is True
        assert lark["config"]["allowed_open_ids"] == [OPEN_ID]
        assert lark["start_problem"] is None
        # A Telegram list written the way it was stored before comes back as a list.
        status, telegram = await _call("PUT", "channel-telegram", {
            "config": {"bot_token_env": "LOOPER_TELEGRAM_BOT_TOKEN", "allowed_user_ids": "7, 8"},
        })
        assert status == 200 and telegram["config"]["allowed_user_ids"] == ["7", "8"]

        status, again = await _call("GET", "channel-lark")
        assert status == 200 and again["config"]["allowed_open_ids"] == [OPEN_ID] and again["start_problem"] is None
        stored = await pool.fetchval("SELECT config FROM channel_configs WHERE channel_id = 'channel-telegram'")
        assert json.loads(stored)["allowed_user_ids"] == ["7", "8"]

    run_with_channel_configs(_test, monkeypatch)


def test_a_bad_id_is_refused_and_nothing_changes(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _test(pool: Any) -> None:
        await _call("PUT", "channel-telegram", {"config": {"allowed_user_ids": ["7"]}})
        for config in ({"allowed_user_ids": ["7", "abc"]}, {"allowed_user_ids": ["-1001234567890"]}, ["7"]):
            status, body = await _call("PUT", "channel-telegram",
                                       {"enabled": True, "config": config})
            assert status == 400, config
            assert body["error"]
        status, body = await _call("PUT", "channel-lark",
                                   {"config": {"allowed_open_ids": ["on_7dab8a3d3cdcc9da365777c7ad535d62"]}})
        assert status == 400 and body["error"] == "invalid credential request"
        _, telegram = await _call("GET", "channel-telegram")
        assert telegram["enabled"] is False and telegram["config"] == {"allowed_user_ids": ["7"]}

    run_with_channel_configs(_test, monkeypatch)


@pytest.mark.usefixtures("lark_cli_stand_in")
def test_an_enabled_channel_with_an_empty_list_is_saved_but_says_it_will_not_start(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _test(pool: Any) -> None:
        status, lark = await _call("PUT", "channel-lark",
                                   {"enabled": True, "config": {"profile": "", "allowed_open_ids": []}})
        assert status == 200 and lark["enabled"] is True
        assert "allowlist is empty" in lark["start_problem"]
        status, _ = await _call("PUT", "channel-none", {"enabled": True})
        assert status == 404

    run_with_channel_configs(_test, monkeypatch)


def test_bot_token_is_stored_masked_and_never_returned(monkeypatch: pytest.MonkeyPatch) -> None:
    """The token goes to channel_secrets, not config: the API shows only has_bot_token, and
    ``***`` keeps it while ``""`` clears it. Mirrors the LLM-provider api_key contract."""
    from looper.db.channel_configs import get_bot_token

    async def _test(pool: Any) -> None:
        # No token to start with.
        status, body = await _call("GET", "channel-telegram")
        assert status == 200 and body.get("has_bot_token") is False
        assert "bot_token" not in body["config"]

        # Store a token.
        status, body = await _call("PUT", "channel-telegram", {"bot_token": "123:secret"})
        assert status == 200 and body["has_bot_token"] is True
        # The token value never leaves the server, in config or anywhere in the view.
        assert "123:secret" not in json.dumps(body)
        assert await get_bot_token("channel-telegram") == "123:secret"
        # It is kept out of channel_configs.config entirely.
        stored = await pool.fetchval("SELECT config FROM channel_configs WHERE channel_id = 'channel-telegram'")
        assert "bot_token" not in json.loads(stored)

        # The mask keeps the stored token; other fields can change alongside it.
        status, body = await _call("PUT", "channel-telegram",
                                   {"bot_token": "***", "config": {"allowed_user_ids": ["7"]}})
        assert status == 200 and body["has_bot_token"] is True
        assert await get_bot_token("channel-telegram") == "123:secret"
        assert body["config"]["allowed_user_ids"] == ["7"]

        # An empty string removes it.
        status, body = await _call("PUT", "channel-telegram", {"bot_token": ""})
        assert status == 200 and body["has_bot_token"] is False
        assert await get_bot_token("channel-telegram") is None

        # A non-string token is refused.
        status, _ = await _call("PUT", "channel-telegram", {"bot_token": 123})
        assert status == 400

    run_with_channel_configs(_test, monkeypatch)


def test_a_rejected_update_does_not_change_the_token(monkeypatch: pytest.MonkeyPatch) -> None:
    """A PUT whose allowlist is invalid returns 400 and leaves the stored token untouched."""
    from looper.db.channel_configs import get_bot_token

    async def _test(pool: Any) -> None:
        status, _ = await _call("PUT", "channel-telegram", {"bot_token": "keep:me"})
        assert status == 200 and await get_bot_token("channel-telegram") == "keep:me"
        # Change the token and break the allowlist in the same request.
        status, _ = await _call("PUT", "channel-telegram",
                                 {"bot_token": "new:token", "config": {"allowed_user_ids": "12,abc"}})
        assert status == 400
        assert await get_bot_token("channel-telegram") == "keep:me"  # unchanged
        # Clearing the token with a bad allowlist must not delete it either.
        status, _ = await _call("PUT", "channel-telegram",
                                 {"bot_token": "", "config": {"allowed_user_ids": "12,abc"}})
        assert status == 400
        assert await get_bot_token("channel-telegram") == "keep:me"  # still there

    run_with_channel_configs(_test, monkeypatch)
