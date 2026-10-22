"""Adding, changing and removing a Manager's bots over ``/api/channel-configs`` (``channels/bot_api.py``) against a
real PostgreSQL, with the stand-in lark-cli for ``lark`` rows and the fake platform for ``lark_bot`` rows.

Runs only when LOOPER_TEST_DATABASE_URL points at a disposable database.
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any

import pytest

from tests.test_channel_configs_db import _run
from tests.test_lark_bot_settings_db import DSN, platform  # noqa: F401 (the fixture)
from tests.test_lark_channel_db import _call as _call_query

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")

TOKEN = "1111111:AAsecret-token-one"
OTHER_TOKEN = "2222222:AAsecret-token-two"
SAME_BOT_NEW_SECRET = "1111111:BBdifferent-secret"
SECRET = "synthetic-app-secret"
ALLOW = {"allowed_user_ids": ["12345"]}


async def _call(method: str, path: str, body: Any = None, query: dict[str, str] | None = None) -> tuple[int, Any]:
    return await _call_query(method, path, body, query)


async def _manager(pool: Any, key: str = "review", **extra: Any) -> str:
    from looper.db import managers

    async with pool.acquire() as conn:
        row = await managers.insert_manager(
            {"id": f"manager-{key}", "name": key.title(), "system_prompt": "p", **extra}, conn=conn)
    return row["id"]


async def _telegram(manager: str, token: str | None = TOKEN, **extra: Any) -> tuple[int, Any]:
    body: dict[str, Any] = {"channel_type": "telegram", "manager_id": manager, "config": dict(ALLOW), **extra}
    if token is not None:
        body["bot_token"] = token
    return await _call("POST", "/api/channel-configs", body)


def test_add_a_telegram_bot_lists_it_after_the_built_in_rows(monkeypatch: pytest.MonkeyPatch) -> None:
    async def test(pool: Any) -> None:
        manager = await _manager(pool)
        status, row = await _telegram(manager)
        assert status == 201, row
        assert row["channel_id"] == "channel-telegram-2" and row["manager_id"] == manager
        assert row["manager_name"] == "Review" and row["builtin"] is False and row["has_bot_token"] is True
        assert row["enabled"] is False and row["display_name"] == "Telegram · Review"
        assert TOKEN not in json.dumps(row)
        status, listed = await _call("GET", "/api/channel-configs")
        ids = [c["channel_id"] for c in listed["configs"]]
        assert ids[-1] == "channel-telegram-2" and set(ids[:-1]) == {"channel-telegram", "channel-lark",
                                                                    "channel-lark_bot"}
        builtin = {c["channel_id"]: c for c in listed["configs"]}
        assert builtin["channel-telegram"]["builtin"] is True and builtin["channel-telegram"]["manager_id"] == "manager"
        assert TOKEN not in json.dumps(listed)

    _run(test, monkeypatch)


def test_the_same_telegram_bot_is_not_added_twice(monkeypatch: pytest.MonkeyPatch) -> None:
    async def test(pool: Any) -> None:
        first, second = await _manager(pool), await _manager(pool, "other")
        assert (await _telegram(first))[0] == 201
        for token in (TOKEN, SAME_BOT_NEW_SECRET):
            status, body = await _telegram(second, token)
            assert status == 409 and "channel-telegram-2" in body["error"], body
            assert token not in json.dumps(body)
        assert await pool.fetchval("SELECT count(*) FROM channel_configs WHERE channel_type = 'telegram'") == 2
        assert (await _telegram(second, OTHER_TOKEN))[0] == 201

    _run(test, monkeypatch)


def test_the_built_in_row_reading_the_environment_counts(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LOOPER_TELEGRAM_BOT_TOKEN", SAME_BOT_NEW_SECRET)

    async def test(pool: Any) -> None:
        manager = await _manager(pool)
        status, body = await _telegram(manager, TOKEN)
        assert status == 409 and "channel-telegram" in body["error"]
        assert TOKEN not in json.dumps(body) and SAME_BOT_NEW_SECRET not in json.dumps(body)

    _run(test, monkeypatch)


def test_refused_adds_write_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    async def test(pool: Any) -> None:
        manager = await _manager(pool)
        await _manager(pool, "gone")
        assert (await _call("DELETE", "/api/managers/manager-gone"))[0] == 200
        before = await pool.fetchval("SELECT count(*) FROM channel_configs")
        assert (await _telegram(manager, None))[0] == 400
        status, body = await _telegram(manager, TOKEN, config={**ALLOW, "bot_token_env": "X"})
        assert status == 400 and "environment" in body["error"]
        assert (await _telegram(manager, TOKEN, surprise=1))[0] == 400
        assert (await _telegram(manager, TOKEN, channel_type="slack"))[0] == 400
        assert (await _telegram(manager, TOKEN, config={"allowed_user_ids": ["nope"]}))[0] == 400
        assert (await _telegram("manager-nobody", TOKEN))[0] == 404
        assert (await _telegram("manager-gone", TOKEN))[0] == 404
        assert (await _call("POST", "/api/channel-configs", [1]))[0] == 400
        assert await pool.fetchval("SELECT count(*) FROM channel_configs") == before
        assert await pool.fetchval("SELECT count(*) FROM channel_secrets") == 0
        await pool.execute("UPDATE managers SET enabled = false WHERE id = $1", manager)
        status, row = await _telegram(manager)
        assert status == 201 and row["manager_id"] == manager

    _run(test, monkeypatch)


def test_lark_cli_bots_name_their_profile(monkeypatch: pytest.MonkeyPatch, lark_cli_stand_in: Path) -> None:
    async def test(pool: Any) -> None:
        manager = await _manager(pool)
        body = {"channel_type": "lark", "manager_id": manager}
        status, answer = await _call("POST", "/api/channel-configs", {**body, "config": {"profile": ""}})
        assert status == 400 and "lark-cli profile" in answer["error"]
        status, answer = await _call("POST", "/api/channel-configs", {**body, "config": {"profile": "other"}})
        assert status == 201, answer
        lark_id = answer["channel_id"]
        assert lark_id == "channel-lark-2" and answer["display_name"] == "Lark (lark-cli) · Review"
        status, answer = await _call("POST", "/api/channel-configs", {**body, "config": {"profile": "other"}})
        assert status == 409 and lark_id in answer["error"]

        status, lark = await _call("GET", "/api/channels/lark/status", query={"channel_id": lark_id})
        assert status == 200 and lark["bot"]["name"] == "OtherBot"
        status, lark = await _call("GET", "/api/channels/lark/status")
        assert status == 200 and lark["bot"]["name"] == "HelperBot"
        status, _ = await _call("GET", "/api/channels/lark/status", query={"channel_id": "channel-lark_bot"})
        assert status == 404
        status, _ = await _call("GET", "/api/channels/lark/status", query={"channel_id": "channel-lark-99"})
        assert status == 404
        status, found = await _call("GET", "/api/channels/lark/people", query={"q": "alan", "channel_id": lark_id})
        assert status == 200 and found["people"]

        # Moving the built-in row onto the added bot's profile clashes too.
        status, answer = await _call("PUT", "/api/channel-configs/channel-lark", {"config": {"profile": "other"}})
        assert status == 409 and lark_id in answer["error"]

    _run(test, monkeypatch)


def test_lark_bot_apps_are_added_once(monkeypatch: pytest.MonkeyPatch, platform: Any) -> None:  # noqa: F811
    async def test(pool: Any) -> None:
        manager = await _manager(pool)
        body = {"channel_type": "lark_bot", "manager_id": manager, "config": {"app_id": "cli_second"},
                "bot_token": SECRET}
        status, answer = await _call("POST", "/api/channel-configs", {**body, "bot_token": None})
        assert status == 400
        status, answer = await _call("POST", "/api/channel-configs", {**body, "config": {}})
        assert status == 400
        status, answer = await _call("POST", "/api/channel-configs", body)
        assert status == 201 and answer["has_bot_token"] is True and SECRET not in json.dumps(answer)
        status, again = await _call("POST", "/api/channel-configs", {**body, "bot_token": "another-secret"})
        assert status == 409 and answer["channel_id"] in again["error"] and "another-secret" not in json.dumps(again)

        status, lark = await _call("GET", "/api/channels/lark_bot/status",
                                   query={"channel_id": answer["channel_id"]})
        assert status == 200 and lark["bot"]["name"] == "HelperBot" and lark["bot"]["app_id"] == "cli_second"
        status, _ = await _call("GET", "/api/channels/lark_bot/status", query={"channel_id": "channel-lark"})
        assert status == 404

    _run(test, monkeypatch)


def test_a_lark_app_is_one_bot_whatever_names_it(monkeypatch: pytest.MonkeyPatch, lark_cli_stand_in: Path) -> None:
    # The built-in lark-cli row names no profile: lark-cli's active one, cli_main, of app cli_main.
    async def test(pool: Any) -> None:
        manager = await _manager(pool)
        lark = {"channel_type": "lark", "manager_id": manager}
        lark_bot = {"channel_type": "lark_bot", "manager_id": manager, "bot_token": SECRET}
        status, answer = await _call("POST", "/api/channel-configs", {**lark, "config": {"profile": "cli_main"}})
        assert status == 409 and "channel-lark)" in answer["error"], answer
        status, answer = await _call("POST", "/api/channel-configs", {**lark_bot, "config": {"app_id": "cli_main"}})
        assert status == 409 and "channel-lark)" in answer["error"], answer
        status, added = await _call("POST", "/api/channel-configs", {**lark_bot, "config": {"app_id": "cli_other"}})
        assert status == 201, added
        status, answer = await _call("POST", "/api/channel-configs", {**lark, "config": {"profile": "other"}})
        assert status == 409 and added["channel_id"] in answer["error"], answer
        status, answer = await _call("PUT", "/api/channel-configs/channel-lark", {"config": {"profile": "other"}})
        assert status == 409 and added["channel_id"] in answer["error"], answer
        assert await pool.fetchval("SELECT count(*) FROM channel_configs WHERE channel_type LIKE 'lark%'") == 3

    _run(test, monkeypatch)


def test_the_built_in_telegram_row_falling_back_to_the_environment_is_checked(
        monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LOOPER_TELEGRAM_BOT_TOKEN", SAME_BOT_NEW_SECRET)
    monkeypatch.setenv("ALT_TELEGRAM_TOKEN", SAME_BOT_NEW_SECRET)

    async def test(pool: Any) -> None:
        path = "/api/channel-configs/channel-telegram"
        assert (await _call("PUT", path, {"bot_token": OTHER_TOKEN}))[0] == 200
        assert (await _telegram(await _manager(pool), TOKEN))[0] == 201  # channel-telegram-2, bot 1111111

        # Without its stored token the built-in row would read the environment's: the added bot's.
        status, body = await _call("PUT", path, {"bot_token": None})
        assert status == 409 and "channel-telegram-2" in body["error"], body
        assert SAME_BOT_NEW_SECRET not in json.dumps(body) and TOKEN not in json.dumps(body)
        assert await pool.fetchval("SELECT bot_token FROM channel_secrets WHERE channel_id = 'channel-telegram'"
                                   ) == OTHER_TOKEN

        monkeypatch.setenv("LOOPER_TELEGRAM_BOT_TOKEN", "3333333:AAthird-env-token")
        assert (await _call("PUT", path, {"bot_token": None}))[0] == 200
        status, body = await _call("PUT", path, {"config": {**ALLOW, "bot_token_env": "ALT_TELEGRAM_TOKEN"}})
        assert status == 409 and "channel-telegram-2" in body["error"], body
        assert "bot_token_env" not in ((await _call("GET", path))[1]["config"] or {})

    _run(test, monkeypatch)


def test_put_keeps_the_manager_and_checks_a_new_token(monkeypatch: pytest.MonkeyPatch) -> None:
    async def test(pool: Any) -> None:
        manager, other = await _manager(pool), await _manager(pool, "other")
        assert (await _telegram(manager))[0] == 201
        assert (await _telegram(other, OTHER_TOKEN))[0] == 201  # channel-telegram-3
        path = "/api/channel-configs/channel-telegram-2"
        status, body = await _call("PUT", path, {"manager_id": "manager", "display_name": "changed"})
        assert status == 400 and "cannot change" in body["error"]
        assert (await _call("GET", path))[1]["display_name"] == "Telegram · Review"
        status, body = await _call("PUT", path, {"manager_id": manager, "display_name": "Mine"})
        assert status == 200 and body["display_name"] == "Mine" and body["manager_id"] == manager

        status, body = await _call("PUT", path, {"bot_token": OTHER_TOKEN})
        assert status == 409 and "channel-telegram-3" in body["error"] and OTHER_TOKEN not in json.dumps(body)
        assert await pool.fetchval("SELECT bot_token FROM channel_secrets WHERE channel_id = 'channel-telegram-2'"
                                   ) == TOKEN
        status, body = await _call("PUT", path, {"bot_token": "1111111:rotated-secret-xx"})
        assert status == 200 and body["has_bot_token"] is True
        status, body = await _call("PUT", path, {"config": {**ALLOW, "bot_token_env": "X"}})
        assert status == 400
        status, body = await _call("PUT", path, {"bot_token": None})
        assert status == 200 and body["has_bot_token"] is False
        status, body = await _call("PUT", "/api/channel-configs/channel-telegram", {"manager_id": "manager"})
        assert status == 200
        status, body = await _call("PUT", "/api/channel-configs/channel-telegram", {"manager_id": manager})
        assert status == 400

    _run(test, monkeypatch)


def test_delete_removes_an_added_bot_and_its_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    async def test(pool: Any) -> None:
        manager = await _manager(pool)
        assert (await _telegram(manager))[0] == 201
        status, body = await _call("DELETE", "/api/channel-configs/channel-telegram")
        assert status == 400 and "built-in telegram channel cannot be removed" in body["error"]
        assert (await _call("GET", "/api/channel-configs/channel-telegram"))[0] == 200

        status, body = await _call("DELETE", f"/api/managers/{manager}")
        assert status == 409 and "remove them in Settings → Channels" in body["error"]

        status, body = await _call("DELETE", "/api/channel-configs/channel-telegram-2")
        assert status == 200 and body == {"deleted": "channel-telegram-2", "restart": True}
        ids = [c["channel_id"] for c in (await _call("GET", "/api/channel-configs"))[1]["configs"]]
        assert "channel-telegram-2" not in ids
        assert await pool.fetchval("SELECT count(*) FROM channel_secrets WHERE channel_id = 'channel-telegram-2'") == 0
        assert (await _call("DELETE", "/api/channel-configs/channel-telegram-2"))[0] == 404
        assert (await _call("DELETE", f"/api/managers/{manager}"))[0] == 200

    _run(test, monkeypatch)


def test_two_adds_of_one_token_at_once_make_one_bot(monkeypatch: pytest.MonkeyPatch) -> None:
    async def test(pool: Any) -> None:
        manager, other = await _manager(pool), await _manager(pool, "other")
        results = await asyncio.gather(_telegram(manager), _telegram(other))
        assert sorted(status for status, _ in results) == [201, 409]
        assert await pool.fetchval("SELECT count(*) FROM channel_configs WHERE channel_type = 'telegram'") == 2

    _run(test, monkeypatch)


def test_no_log_line_carries_a_token(monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
                                     lark_cli_stand_in: Path, platform: Any) -> None:  # noqa: F811
    from looper.agents import log_redaction

    caplog.set_level(logging.DEBUG)

    async def test(pool: Any) -> None:
        manager = await _manager(pool)
        await _telegram(manager)
        await _telegram(manager)
        await _call("PUT", "/api/channel-configs/channel-telegram-2", {"bot_token": OTHER_TOKEN})
        await _call("POST", "/api/channel-configs", {
            "channel_type": "lark_bot", "manager_id": manager, "config": {"app_id": "cli_third"},
            "bot_token": SECRET})
        await _call("DELETE", "/api/channel-configs/channel-telegram-2")

    _run(test, monkeypatch)
    formatter = log_redaction.RedactingFormatter("%(message)s")
    for record in caplog.records:
        text = formatter.format(record)
        for secret in (TOKEN, OTHER_TOKEN, SECRET):
            assert secret not in text
