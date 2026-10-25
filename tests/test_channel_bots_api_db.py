"""Adding, changing and removing a Manager's bots over ``/api/v1/channel_configs/*`` (``channels/bot_api.py``) against a
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

from tests.db_support import DSN, run_with_channel_configs
from tests.test_lark_bot_settings_db import platform  # noqa: F401 (the fixture)
from tests.test_lark_channel_db import _call as _call_query
from tests.test_api_v1_managers_templates import _call as _manager_call

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
    return await _call("POST", "channel_configs", body)


@pytest.mark.parametrize("kind,operation,unavailable", [
    ("lark", "add", "candidate"), ("lark", "add", "empty"), ("lark", "add", "existing"),
    ("lark_bot", "add", "existing"), ("lark", "update", "candidate"),
    ("lark", "move", "candidate"), ("lark", "update", "existing"),
    ("lark_bot", "update", "existing"), ("lark_bot", "update_config", "existing"),
    ("lark", "metadata", "candidate"),
    ("lark_bot", "metadata", "existing"),
    ("lark", "disable_identity", "candidate"), ("lark_bot", "disable_identity", "existing"),
    ("lark_bot", "disable_token", "existing"), ("lark_bot", "remove_token", "existing"),
    ("lark_bot", "disable_domain", "existing"),
])
def test_unknown_lark_identity_refuses_writes_until_recovery(
    monkeypatch: pytest.MonkeyPatch, kind: str, operation: str, unavailable: str,
) -> None:
    from looper.agents.channels import lark_cli

    broken, duplicate = False, False

    async def identity(profile: str | None = None, *, fresh: bool = False) -> Any:
        assert fresh
        profile = "candidate" if profile == "alias" else profile
        if broken and (profile == unavailable or unavailable == "empty" and profile == "candidate"):
            if unavailable == "empty":
                return lark_cli.Identity(profile, "", "Bot", "ou_bot")
            raise lark_cli.LarkCliError(f"failed with {SECRET} at /private/profile")
        app = "cli_shared" if profile != "existing" or duplicate else "cli_existing"
        if kind == "lark_bot" and operation == "disable_identity" and duplicate:
            app = "cli_changed"
        return lark_cli.Identity(profile or "", app, "Bot", "ou_bot")

    monkeypatch.setattr(lark_cli, "identity", identity)

    async def test(pool: Any) -> None:
        await pool.execute("UPDATE channel_configs SET config = $1 WHERE channel_id = 'channel-lark'",
                           json.dumps({"profile": "existing"}))
        body = {"channel_type": kind, "manager_id": await _manager(pool),
                "config": {"profile": "candidate"} if kind == "lark" else {"app_id": "cli_shared"}}
        if kind == "lark_bot":
            body["bot_token"] = SECRET
        path, method = "channel_configs", "POST"
        if operation != "add":
            status, saved = await _call(method, path, body)
            assert status == 200, saved
            path, method = saved["channel_id"], "PUT"
            body = ({"bot_token": "synthetic-replacement-secret"} if kind == "lark_bot" else
                    {"config": {"profile": "alias" if operation == "move" else "candidate",
                                "description": "changed"}})
            if operation == "metadata":
                body = {"enabled": True, "display_name": "Changed"}
            elif operation == "update_config":
                body["config"] = {"app_id": "cli_shared", "description": "changed"}
            elif operation == "disable_identity":
                body = {"enabled": False, "config": (
                    {"profile": "alias"} if kind == "lark" else {"app_id": "cli_changed"})}
            elif operation in ("disable_token", "remove_token"):
                body = {"enabled": False, "bot_token": (
                    None if operation == "remove_token" else "synthetic-replacement-secret")}
            elif operation == "disable_domain":
                body = {"enabled": False, "config": {"app_id": "cli_shared", "domain": "lark"}}

        async def state() -> Any:
            return [await pool.fetch(f"SELECT * FROM {table} ORDER BY channel_id")
                    for table in ("channel_configs", "channel_secrets")]

        nonlocal broken, duplicate
        before = await state()
        broken, duplicate = True, True
        status, answer = await _call(method, path, body)
        assert status == 409 and answer["code"] == "conflict", answer
        assert "cannot verify Lark app identity" in answer["error"], answer
        assert SECRET not in json.dumps(answer) and "/private/profile" not in json.dumps(answer)
        assert await state() == before
        # Once lookup works, the same app is a conflict across profiles and channel types.
        broken = False
        status, answer = await _call(method, path, body)
        assert status == 409 and answer["code"] == "conflict", answer
        assert "this bot is already added" in answer["error"], answer
        assert await state() == before
        duplicate = False
        status, saved = await _call(method, path, body)
        assert status == 200, saved
        status, readback = await _call("GET", saved["channel_id"])
        assert status == 200 and readback["config"] == saved["config"]
        assert readback["enabled"] == saved["enabled"]
        if kind == "lark_bot":
            expected = (None if operation == "remove_token" else "synthetic-replacement-secret"
                        if operation in ("update", "update_config", "disable_token") else SECRET)
            assert await pool.fetchval("SELECT bot_token FROM channel_secrets WHERE channel_id = $1",
                                       saved["channel_id"]) == expected

    run_with_channel_configs(test, monkeypatch)


@pytest.mark.parametrize("kind,fields", [
    ("lark", {"enabled": False}),
    ("lark", {"enabled": False, "full_config": True}),
    ("lark_bot", {"enabled": False}),
    ("lark_bot", {"enabled": False, "full_config": True, "bot_token": "***"}),
    ("lark_bot", {"enabled": False, "full_config": True, "bot_token": SECRET}),
    ("lark", {"enabled": False, "builtin": True, "full_config": True}),
])
def test_unchanged_lark_bot_can_be_disabled_without_authentication(monkeypatch, kind, fields):
    from looper.agents.channels import lark_cli

    broken = False

    async def identity(profile=None, *, fresh=False):
        assert fresh
        if broken:
            raise lark_cli.LarkCliError(f"authentication failed: {SECRET}")
        return lark_cli.Identity(profile or "", "cli_added" if profile else "cli_builtin", "Bot", "ou_bot")

    monkeypatch.setattr(lark_cli, "identity", identity)

    async def test(pool):
        config = ({"profile": "candidate"} if kind == "lark" else {"app_id": "cli_added", "domain": "feishu"})
        config.update(allowed_open_ids=["ou_sender0000001"], description="Saved")
        body = {"channel_type": kind, "manager_id": await _manager(pool), "config": config, "enabled": True}
        if kind == "lark_bot":
            body["bot_token"] = SECRET
        request = dict(fields)
        if request.pop("builtin", False):
            body["config"]["profile"] = ""
            status, saved = await _call("PUT", "channel-lark", {"enabled": True, "config": body["config"]})
        else:
            status, saved = await _call("POST", "channel_configs", body)
        assert status == 200 and saved["enabled"] is True, saved
        if request.pop("full_config", False):
            request.update(config=saved["config"], display_name=saved["display_name"], manager_id=saved["manager_id"])
        nonlocal broken
        broken = True
        status, answer = await _call("PUT", saved["channel_id"], request)
        assert status == 200 and answer["enabled"] is False, answer
        status, readback = await _call("GET", saved["channel_id"])
        assert status == 200 and readback == answer
        assert readback["config"] == saved["config"] and readback["manager_id"] == saved["manager_id"]
        assert await pool.fetchval("SELECT enabled FROM channel_configs WHERE channel_id = $1",
                                   saved["channel_id"]) is False
        assert await pool.fetchval("SELECT bot_token FROM channel_secrets WHERE channel_id = $1",
                                   saved["channel_id"]) == (SECRET if kind == "lark_bot" else None)

    run_with_channel_configs(test, monkeypatch)


@pytest.mark.parametrize("changed", ["profile", "token", "allowlist", "binding"])
def test_lark_disable_admission_uses_identity_and_token_read_under_lock(monkeypatch, changed):
    from looper.agents.channels import bot_api, lark_cli

    broken = False

    async def identity(profile=None, *, fresh=False):
        if broken:
            raise lark_cli.LarkCliError("authentication failed")
        return lark_cli.Identity(profile or "", "cli_" + (profile or "builtin"), "Bot", "ou_bot")

    monkeypatch.setattr(lark_cli, "identity", identity)

    async def test(pool):
        kind = "lark_bot" if changed == "token" else "lark"
        body = {"channel_type": kind, "manager_id": await _manager(pool), "enabled": True,
                "config": {"profile": "candidate"} if kind == "lark" else {"app_id": "cli_added"}}
        if changed in ("allowlist", "binding"):
            body["config"]["allowed_open_ids"] = ["ou_sender0000001"]
        if kind == "lark_bot":
            body["bot_token"] = SECRET
        status, saved = await _call("POST", "channel_configs", body)
        assert status == 200, saved
        real = bot_api.db_channel_configs.get_config
        raced = []
        reads = []

        async def get_config(channel_id, **kwargs):
            row = await real(channel_id, **kwargs)
            if not kwargs:
                reads.append(channel_id)
            # A full-config request first reads in the v1 validator, then in update_bot.
            # Change the persisted identity after update_bot's initial read, before its lock.
            if not raced and not kwargs and len(reads) == 2:
                raced.append(True)
                if changed == "binding":
                    await pool.execute("UPDATE channel_configs SET config = config || $1::jsonb "
                                       "WHERE channel_id = $2",
                                       json.dumps({"allowlist_app_id": "cli_replacement"}), channel_id)
                else:
                    fields = ({"config": {"profile": "replacement"}} if changed == "profile" else
                              {"config": {**saved["config"], "allowed_open_ids": ["ou_sender0000002"]}}
                              if changed == "allowlist" else {"bot_token": "synthetic-replacement-secret"})
                    status, answer = await _call("PUT", channel_id, fields)
                    assert status == 200, answer
                nonlocal broken
                broken = True
            return row

        monkeypatch.setattr(bot_api.db_channel_configs, "get_config", get_config)
        fields = {"enabled": False, "config": saved["config"]}
        if kind == "lark_bot":
            fields["bot_token"] = SECRET
        status, answer = await _call("PUT", saved["channel_id"], fields)
        assert raced and status == 409 and "cannot verify Lark app identity" in answer["error"], answer
        readback = await real(saved["channel_id"])
        assert readback["enabled"] is True
        expected = dict(saved["config"])
        if changed == "profile":
            expected = {"profile": "replacement"}
        elif changed == "allowlist":
            expected["allowed_open_ids"] = ["ou_sender0000002"]
        elif changed == "binding":
            expected["allowlist_app_id"] = "cli_replacement"
        assert readback["config"] == expected
        assert await pool.fetchval("SELECT bot_token FROM channel_secrets WHERE channel_id = $1",
                                   saved["channel_id"]) == ("synthetic-replacement-secret" if changed == "token" else None)

    run_with_channel_configs(test, monkeypatch)


@pytest.mark.parametrize("kind,change", [
    ("lark", "replace"), ("lark", "clear"), ("lark", "normalized"), ("lark", "reorder"),
    ("lark_bot", "normalized"), ("lark_bot", "replace"),
])
def test_full_config_disable_preserves_sender_admission_over_http(monkeypatch, tmp_path, kind, change):
    from looper.agents.channels import lark_cli
    from looper.agents.ops.clients import ClientOps
    from looper.agents.ops.service import Platform
    from tests.test_api_v1_auth import TOKEN as API_TOKEN, _bearer, _send, _serving

    broken = False

    async def identity(profile=None, *, fresh=False):
        assert fresh
        if broken:
            raise lark_cli.LarkCliError(f"authentication failed: {SECRET}")
        return lark_cli.Identity(profile or "", "cli_added" if profile else "cli_builtin", "Bot", "ou_bot")

    monkeypatch.setattr(lark_cli, "identity", identity)

    async def test(pool):
        manager = await _manager(pool)
        async with _serving(tmp_path, listen=True) as server:
            server._client_ops = ClientOps(Platform(bus=None, task_router=server._task_router,
                                                   runtime_root=tmp_path))

            async def call(action, body):
                status, _, answer = await _send(server, f"POST /api/v1/channel_configs/{action}",
                                                _bearer(API_TOKEN), json.dumps(body))
                assert SECRET not in json.dumps(answer)
                return status, answer

            allowed = ["ou_sender0000001", "ou_sender0000002"]
            config = {"allowed_open_ids": allowed, "description": "Saved"}
            config.update({"profile": "candidate"} if kind == "lark" else {"app_id": "cli_added"})
            body = {"channel_type": kind, "manager_id": manager, "config": config, "enabled": True}
            if kind == "lark_bot":
                body["bot_token"] = SECRET
            status, answer = await call("create", body)
            assert status == 200, answer
            saved = answer["result"]
            assert saved["config"]["allowlist_app_id"] == "cli_added"
            before = [await pool.fetch(f"SELECT * FROM {table} ORDER BY channel_id")
                      for table in ("channel_configs", "channel_secrets")]
            config = {**saved["config"], "allowed_open_ids": {
                "replace": ["ou_sender0000003"], "clear": [],
                "normalized": f" {allowed[0]}, {allowed[1]}, {allowed[0]}, ", "reorder": allowed[::-1],
            }[change]}
            if change == "normalized":
                # The server owns the binding; an omitted echo must retain it for the same sender set.
                config.pop("allowlist_app_id")
            fields = {"enabled": False, "config": config, "display_name": "Offline", "manager_id": manager}
            if kind == "lark_bot":
                fields["bot_token"] = "***"
            nonlocal broken
            broken = True
            status, answer = await call("update", {"channel_id": saved["channel_id"], "fields": fields})
            refused = kind == "lark" and change in ("replace", "clear")
            if refused:
                assert status == 409 and answer["error"]["code"] == "conflict", answer
                assert "cannot verify Lark app identity" in answer["error"]["message"]
                assert [await pool.fetch(f"SELECT * FROM {table} ORDER BY channel_id")
                        for table in ("channel_configs", "channel_secrets")] == before
            else:
                assert status == 200 and answer["result"]["enabled"] is False, answer
                expected_ids = ["ou_sender0000003"] if change == "replace" else allowed
                assert set(answer["result"]["config"]["allowed_open_ids"]) == set(expected_ids)
                assert answer["result"]["config"]["allowlist_app_id"] == "cli_added"
            status, readback = await call("get", {"channel_id": saved["channel_id"]})
            assert status == 200 and readback["result"] == (saved if refused else answer["result"])
            assert await pool.fetchval("SELECT bot_token FROM channel_secrets WHERE channel_id = $1",
                                       saved["channel_id"]) == (SECRET if kind == "lark_bot" else None)
            if refused:
                # Prove that admission, rather than malformed input, refused the offline save.
                broken = False
                status, recovered = await call("update", {"channel_id": saved["channel_id"], "fields": fields})
                assert status == 200 and recovered["result"]["enabled"] is False, recovered
                config = recovered["result"]["config"]
                assert config["allowed_open_ids"] == (["ou_sender0000003"] if change == "replace" else [])
                assert config.get("allowlist_app_id") == ("cli_added" if change == "replace" else None)
                status, readback = await call("get", {"channel_id": saved["channel_id"]})
                assert status == 200 and readback["result"] == recovered["result"]

    run_with_channel_configs(test, monkeypatch)


def test_lark_identity_admission_over_http(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from looper.agents.channels import lark_cli
    from looper.agents.ops.clients import ClientOps
    from looper.agents.ops.service import Platform
    from tests.test_api_v1_auth import TOKEN as API_TOKEN, _bearer, _send, _serving

    broken = True

    async def identity(profile: str | None = None, *, fresh: bool = False) -> Any:
        if broken:
            raise lark_cli.LarkCliError(f"{SECRET} at /private/profile")
        return lark_cli.Identity(profile or "", "cli_main", "Bot", "ou_bot")

    monkeypatch.setattr(lark_cli, "identity", identity)

    async def test(pool: Any) -> None:
        manager = await _manager(pool)
        async with _serving(tmp_path, listen=True) as server:
            server._client_ops = ClientOps(Platform(bus=None, task_router=server._task_router,
                                                   runtime_root=tmp_path))

            async def call(action: str, body: Any) -> tuple[int, Any]:
                status, _, answer = await _send(server, f"POST /api/v1/channel_configs/{action}",
                                                _bearer(API_TOKEN), json.dumps(body))
                assert SECRET not in json.dumps(answer) and "/private/profile" not in json.dumps(answer)
                return status, answer

            # The paired built-ins do not compete, even when lark-cli is unconfigured.
            status, answer = await call("update", {"channel_id": "channel-lark_bot", "fields": {
                "config": {"app_id": "cli_builtin"}, "bot_token": SECRET}})
            assert status == 200, answer
            body = {"channel_type": "lark_bot", "manager_id": manager,
                    "config": {"app_id": "cli_added"}, "bot_token": SECRET}
            status, answer = await call("create", body)
            assert status == 409 and answer["error"]["code"] == "conflict", answer
            assert "cannot verify Lark app identity" in answer["error"]["message"], answer
            nonlocal broken
            broken = False
            status, answer = await call("create", body)
            assert status == 200, answer
            saved = answer["result"]
            status, answer = await call("get", {"channel_id": saved["channel_id"]})
            assert status == 200 and answer["result"] == saved

    run_with_channel_configs(test, monkeypatch)


def test_add_a_telegram_bot_lists_it_after_the_built_in_rows(monkeypatch: pytest.MonkeyPatch) -> None:
    async def test(pool: Any) -> None:
        manager = await _manager(pool)
        status, row = await _telegram(manager)
        assert status == 200, row
        assert row["channel_id"] == "channel-telegram-2" and row["manager_id"] == manager
        assert row["manager_name"] == "Review" and row["builtin"] is False and row["has_bot_token"] is True
        assert row["enabled"] is False and row["display_name"] == "Telegram · Review"
        assert TOKEN not in json.dumps(row)
        status, listed = await _call("GET", "channel_configs")
        ids = [c["channel_id"] for c in listed["configs"]]
        assert ids[-1] == "channel-telegram-2" and set(ids[:-1]) == {"channel-telegram", "channel-lark",
                                                                    "channel-lark_bot"}
        builtin = {c["channel_id"]: c for c in listed["configs"]}
        assert builtin["channel-telegram"]["builtin"] is True and builtin["channel-telegram"]["manager_id"] == "manager"
        assert TOKEN not in json.dumps(listed)

    run_with_channel_configs(test, monkeypatch)


def test_the_same_telegram_bot_is_not_added_twice(monkeypatch: pytest.MonkeyPatch) -> None:
    async def test(pool: Any) -> None:
        first, second = await _manager(pool), await _manager(pool, "other")
        assert (await _telegram(first))[0] == 200
        for token in (TOKEN, SAME_BOT_NEW_SECRET):
            status, body = await _telegram(second, token)
            assert status == 409 and body["error"] == (
                "this bot is already added; update the existing bot instead"), body
            assert token not in json.dumps(body)
        assert await pool.fetchval("SELECT count(*) FROM channel_configs WHERE channel_type = 'telegram'") == 2
        assert (await _telegram(second, OTHER_TOKEN))[0] == 200

    run_with_channel_configs(test, monkeypatch)


def test_the_built_in_row_reading_the_environment_counts(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LOOPER_TELEGRAM_BOT_TOKEN", SAME_BOT_NEW_SECRET)

    async def test(pool: Any) -> None:
        manager = await _manager(pool)
        status, body = await _telegram(manager, TOKEN)
        assert status == 409 and body["error"] == (
            "this bot is already added; update the existing bot instead")
        assert TOKEN not in json.dumps(body) and SAME_BOT_NEW_SECRET not in json.dumps(body)

    run_with_channel_configs(test, monkeypatch)


def test_the_built_in_row_whose_bot_token_env_is_unset_counts_the_standard_variable(
        monkeypatch: pytest.MonkeyPatch) -> None:
    # As on main, the built-in row whose bot_token_env names an unset variable runs LOOPER_TELEGRAM_BOT_TOKEN's bot.
    monkeypatch.setenv("LOOPER_TELEGRAM_BOT_TOKEN", SAME_BOT_NEW_SECRET)
    monkeypatch.delenv("UNSET_TELEGRAM_TOKEN", raising=False)

    async def test(pool: Any) -> None:
        status, _ = await _call("PUT", "channel-telegram",
                                {"config": {**ALLOW, "bot_token_env": "UNSET_TELEGRAM_TOKEN"}})
        assert status == 200
        status, body = await _telegram(await _manager(pool), TOKEN)
        assert status == 409 and body["error"] == (
            "this bot is already added; update the existing bot instead")

    run_with_channel_configs(test, monkeypatch)


def test_refused_adds_write_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    async def test(pool: Any) -> None:
        manager = await _manager(pool)
        await _manager(pool, "gone")
        assert (await _manager_call("managers.delete", {"manager_id": "manager-gone"}))[0] == 200
        before = await pool.fetchval("SELECT count(*) FROM channel_configs")
        assert (await _telegram(manager, None))[0] == 400
        status, body = await _telegram(manager, TOKEN, config={**ALLOW, "bot_token_env": "X"})
        assert status == 400 and body["error"] == (
            "invalid channel configuration; check the sender allowlist and bot settings")
        assert (await _telegram(manager, TOKEN, surprise=1))[0] == 400
        assert (await _telegram(manager, TOKEN, channel_type="slack"))[0] == 400
        assert (await _telegram(manager, TOKEN, config={"allowed_user_ids": ["nope"]}))[0] == 400
        assert (await _telegram("manager-nobody", TOKEN))[0] == 404
        assert (await _telegram("manager-gone", TOKEN))[0] == 404
        assert (await _call("POST", "channel_configs", [1]))[0] == 400
        assert await pool.fetchval("SELECT count(*) FROM channel_configs") == before
        assert await pool.fetchval("SELECT count(*) FROM channel_secrets") == 0
        await pool.execute("UPDATE managers SET enabled = false WHERE id = $1", manager)
        status, row = await _telegram(manager)
        assert status == 200 and row["manager_id"] == manager

    run_with_channel_configs(test, monkeypatch)


def test_lark_cli_bots_name_their_profile(monkeypatch: pytest.MonkeyPatch, lark_cli_stand_in: Path) -> None:
    async def test(pool: Any) -> None:
        manager = await _manager(pool)
        body = {"channel_type": "lark", "manager_id": manager}
        status, answer = await _call("POST", "channel_configs", {**body, "config": {"profile": ""}})
        assert status == 400 and answer["error"] == (
            "name the lark-cli profile of this bot: the active profile belongs to the built-in Lark channel")
        status, answer = await _call("POST", "channel_configs", {**body, "config": {"profile": "other"}})
        assert status == 200, answer
        lark_id = answer["channel_id"]
        assert lark_id == "channel-lark-2" and answer["display_name"] == "Lark (lark-cli) · Review"
        status, answer = await _call("POST", "channel_configs", {**body, "config": {"profile": "other"}})
        assert status == 409 and answer["error"] == (
            "this bot is already added; update the existing bot instead")

        status, lark = await _call("POST", "/api/v1/channels/status", {"channel_type": "lark", "channel_id": lark_id})
        assert status == 200 and lark["bot"]["name"] == "OtherBot"
        status, lark = await _call("POST", "/api/v1/channels/status", {"channel_type": "lark"})
        assert status == 200 and lark["bot"]["name"] == "HelperBot"
        status, _ = await _call("POST", "/api/v1/channels/status",
                                {"channel_type": "lark", "channel_id": "channel-lark_bot"})
        assert status == 404
        status, _ = await _call("POST", "/api/v1/channels/status",
                                {"channel_type": "lark", "channel_id": "channel-lark-99"})
        assert status == 404
        status, found = await _call("POST", "/api/v1/channels/people",
                                    {"channel_type": "lark", "q": "alan", "channel_id": lark_id})
        assert status == 200 and found["people"]

        # Moving the built-in row onto the added bot's profile clashes too.
        status, answer = await _call("PUT", "channel-lark", {"config": {"profile": "other"}})
        assert status == 409 and answer["error"] == (
            "this bot is already added; update the existing bot instead")

    run_with_channel_configs(test, monkeypatch)


def test_an_added_lark_cli_bot_keeps_naming_its_profile(monkeypatch: pytest.MonkeyPatch,
                                                         lark_cli_stand_in: Path) -> None:
    world_path = lark_cli_stand_in.with_name("world.json")
    world = json.loads(world_path.read_text())
    world["profiles"]["out"]["signed_in"] = True
    world_path.write_text(json.dumps(world))

    async def test(pool: Any) -> None:
        manager = await _manager(pool)
        status, answer = await _call("POST", "channel_configs",
                                     {"channel_type": "lark", "manager_id": manager, "config": {"profile": "other"}})
        assert status == 200, answer
        lark_id = answer["channel_id"]
        # With the built-in row on another profile, an empty one clashes with no row, and is still refused.
        status, answer = await _call("PUT", "channel-lark", {"config": {"profile": "out"}})
        assert status == 200, answer
        status, answer = await _call("PUT", f"{lark_id}", {"config": {"profile": ""}})
        assert status == 400 and answer["error"] == (
            "name the lark-cli profile of this bot: the active profile belongs to the built-in Lark channel"), answer
        status, answer = await _call("GET", f"{lark_id}")
        assert answer["config"]["profile"] == "other"
        # The built-in row may still use lark-cli's active profile.
        status, answer = await _call("PUT", "channel-lark", {"config": {"profile": ""}})
        assert status == 200, answer

    run_with_channel_configs(test, monkeypatch)


def test_lark_bot_apps_are_added_once(monkeypatch: pytest.MonkeyPatch, lark_cli_stand_in: Path,
                                    platform: Any) -> None:  # noqa: F811
    async def test(pool: Any) -> None:
        manager = await _manager(pool)
        body = {"channel_type": "lark_bot", "manager_id": manager, "config": {"app_id": "cli_second"},
                "bot_token": SECRET}
        status, answer = await _call("POST", "channel_configs", {**body, "bot_token": None})
        assert status == 400
        status, answer = await _call("POST", "channel_configs", {**body, "config": {}})
        assert status == 400
        status, answer = await _call("POST", "channel_configs", body)
        assert status == 200 and answer["has_bot_token"] is True and SECRET not in json.dumps(answer)
        status, again = await _call("POST", "channel_configs", {**body, "bot_token": "another-secret"})
        assert status == 409 and again["error"] == (
            "this bot is already added; update the existing bot instead") and "another-secret" not in json.dumps(again)

        status, lark = await _call("POST", "/api/v1/channels/status",
                                   {"channel_type": "lark_bot", "channel_id": answer["channel_id"]})
        assert status == 200 and lark["bot"]["name"] == "HelperBot" and lark["bot"]["app_id"] == "cli_second"
        status, _ = await _call("POST", "/api/v1/channels/status",
                                {"channel_type": "lark_bot", "channel_id": "channel-lark"})
        assert status == 404

    run_with_channel_configs(test, monkeypatch)


def test_a_lark_app_is_one_bot_whatever_names_it(monkeypatch: pytest.MonkeyPatch, lark_cli_stand_in: Path) -> None:
    # The built-in lark-cli row names no profile: lark-cli's active one, cli_main, of app cli_main.
    async def test(pool: Any) -> None:
        manager = await _manager(pool)
        lark = {"channel_type": "lark", "manager_id": manager}
        lark_bot = {"channel_type": "lark_bot", "manager_id": manager, "bot_token": SECRET}
        status, answer = await _call("POST", "channel_configs", {**lark, "config": {"profile": "cli_main"}})
        assert status == 409 and answer["error"] == (
            "this bot is already added; update the existing bot instead"), answer
        status, answer = await _call("POST", "channel_configs", {**lark_bot, "config": {"app_id": "cli_main"}})
        assert status == 409 and answer["error"] == (
            "this bot is already added; update the existing bot instead"), answer
        status, added = await _call("POST", "channel_configs", {**lark_bot, "config": {"app_id": "cli_other"}})
        assert status == 200, added
        status, answer = await _call("POST", "channel_configs", {**lark, "config": {"profile": "other"}})
        assert status == 409 and answer["error"] == (
            "this bot is already added; update the existing bot instead"), answer
        status, answer = await _call("PUT", "channel-lark", {"config": {"profile": "other"}})
        assert status == 409 and answer["error"] == (
            "this bot is already added; update the existing bot instead"), answer
        assert await pool.fetchval("SELECT count(*) FROM channel_configs WHERE channel_type LIKE 'lark%'") == 3

    run_with_channel_configs(test, monkeypatch)


def test_the_built_in_lark_bot_row_may_act_as_the_built_in_lark_cli_rows_app(
        monkeypatch: pytest.MonkeyPatch, lark_cli_stand_in: Path, platform: Any) -> None:  # noqa: F811
    # The built-in lark-cli row acts as cli_main (lark-cli's active profile); the built-in bot token row may too, as
    # today, while an added bot of cli_main may not.
    async def test(pool: Any) -> None:
        status, answer = await _call("PUT", "channel-lark_bot",
                                     {"config": {"app_id": "cli_main"}, "bot_token": SECRET})
        assert status == 200, answer
        status, answer = await _call("PUT", "channel-lark_bot", {"bot_token": "new-secret"})
        assert status == 200, answer
        status, answer = await _call("PUT", "channel-lark", {"config": {"profile": ""}})
        assert status == 200, answer
        manager = await _manager(pool)
        status, answer = await _call("POST", "channel_configs", {
            "channel_type": "lark_bot", "manager_id": manager, "config": {"app_id": "cli_main"}, "bot_token": SECRET})
        assert status == 409 and answer["error"] == (
            "this bot is already added; update the existing bot instead"), answer

    run_with_channel_configs(test, monkeypatch)


def test_the_built_in_telegram_row_falling_back_to_the_environment_is_checked(
        monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LOOPER_TELEGRAM_BOT_TOKEN", SAME_BOT_NEW_SECRET)
    monkeypatch.setenv("ALT_TELEGRAM_TOKEN", SAME_BOT_NEW_SECRET)

    async def test(pool: Any) -> None:
        path = "channel-telegram"
        assert (await _call("PUT", path, {"bot_token": OTHER_TOKEN}))[0] == 200
        assert (await _telegram(await _manager(pool), TOKEN))[0] == 200  # channel-telegram-2, bot 1111111

        # Without its stored token the built-in row would read the environment's: the added bot's.
        status, body = await _call("PUT", path, {"bot_token": None})
        assert status == 409 and body["error"] == (
            "this bot is already added; update the existing bot instead"), body
        assert SAME_BOT_NEW_SECRET not in json.dumps(body) and TOKEN not in json.dumps(body)
        assert await pool.fetchval("SELECT bot_token FROM channel_secrets WHERE channel_id = 'channel-telegram'"
                                   ) == OTHER_TOKEN

        monkeypatch.setenv("LOOPER_TELEGRAM_BOT_TOKEN", "3333333:AAthird-env-token")
        assert (await _call("PUT", path, {"bot_token": None}))[0] == 200
        status, body = await _call("PUT", path, {"config": {**ALLOW, "bot_token_env": "ALT_TELEGRAM_TOKEN"}})
        assert status == 409 and body["error"] == (
            "this bot is already added; update the existing bot instead"), body
        assert "bot_token_env" not in ((await _call("GET", path))[1]["config"] or {})

    run_with_channel_configs(test, monkeypatch)


def test_a_token_removal_is_checked_against_a_bot_token_env_changed_meanwhile(
        monkeypatch: pytest.MonkeyPatch) -> None:
    # One request removes the built-in Telegram row's token while another points its bot_token_env at the added
    # bot's token: whichever lands second must see the other's change, not the row as it read it before the lock.
    monkeypatch.setenv("LOOPER_TELEGRAM_BOT_TOKEN", "3333333:AAthird-env-token")
    monkeypatch.setenv("ALT_TELEGRAM_TOKEN", SAME_BOT_NEW_SECRET)

    async def test(pool: Any) -> None:
        from looper.agents.channels import bot_api

        path = "channel-telegram"
        assert (await _call("PUT", path, {"bot_token": OTHER_TOKEN}))[0] == 200
        assert (await _telegram(await _manager(pool), TOKEN))[0] == 200  # channel-telegram-2, bot 1111111
        real = bot_api.db_channel_configs.get_config
        raced: list[int] = []

        async def get_config(channel_id: str, **kwargs: Any) -> Any:
            row = await real(channel_id, **kwargs)
            if not raced and not kwargs:
                raced.append(1)
                status, body = await _call("PUT", path, {"config": {**ALLOW, "bot_token_env": "ALT_TELEGRAM_TOKEN"}})
                assert status == 200, body
            return row

        monkeypatch.setattr(bot_api.db_channel_configs, "get_config", get_config)
        status, body = await _call("PUT", path, {"bot_token": None})
        assert raced and status == 409 and body["error"] == (
            "this bot is already added; update the existing bot instead"), body
        assert await pool.fetchval("SELECT bot_token FROM channel_secrets WHERE channel_id = 'channel-telegram'"
                                   ) == OTHER_TOKEN

    run_with_channel_configs(test, monkeypatch)


def test_put_keeps_the_manager_and_checks_a_new_token(monkeypatch: pytest.MonkeyPatch) -> None:
    async def test(pool: Any) -> None:
        manager, other = await _manager(pool), await _manager(pool, "other")
        assert (await _telegram(manager))[0] == 200
        assert (await _telegram(other, OTHER_TOKEN))[0] == 200  # channel-telegram-3
        path = "channel-telegram-2"
        status, body = await _call("PUT", path, {"manager_id": "manager", "display_name": "changed"})
        assert status == 400 and body["error"] == (
            "a bot's Manager cannot change: add a new bot for the other Manager and remove this one")
        assert (await _call("GET", path))[1]["display_name"] == "Telegram · Review"
        status, body = await _call("PUT", path, {"manager_id": manager, "display_name": "Mine"})
        assert status == 200 and body["display_name"] == "Mine" and body["manager_id"] == manager

        status, body = await _call("PUT", path, {"bot_token": OTHER_TOKEN})
        assert status == 409 and body["error"] == (
            "this bot is already added; update the existing bot instead") and OTHER_TOKEN not in json.dumps(body)
        assert await pool.fetchval("SELECT bot_token FROM channel_secrets WHERE channel_id = 'channel-telegram-2'"
                                   ) == TOKEN
        status, body = await _call("PUT", path, {"bot_token": "1111111:rotated-secret-xx"})
        assert status == 200 and body["has_bot_token"] is True
        status, body = await _call("PUT", path, {"config": {**ALLOW, "bot_token_env": "X"}})
        assert status == 400
        status, body = await _call("PUT", path, {"bot_token": None})
        assert status == 200 and body["has_bot_token"] is False
        status, body = await _call("PUT", "channel-telegram", {"manager_id": "manager"})
        assert status == 200
        status, body = await _call("PUT", "channel-telegram", {"manager_id": manager})
        assert status == 400

    run_with_channel_configs(test, monkeypatch)


def test_delete_removes_an_added_bot_and_its_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    async def test(pool: Any) -> None:
        manager = await _manager(pool)
        assert (await _telegram(manager))[0] == 200
        status, body = await _call("DELETE", "channel-telegram")
        assert status == 400 and "built-in channel cannot be removed" in body["error"]
        assert (await _call("GET", "channel-telegram"))[0] == 200

        status, body = await _manager_call("managers.delete", {"manager_id": manager})
        assert status == 409 and "remove them in Settings → Channels" in body["error"]

        status, body = await _call("DELETE", "channel-telegram-2")
        assert status == 200 and body == {"deleted": "channel-telegram-2", "restart": True}
        ids = [c["channel_id"] for c in (await _call("GET", "channel_configs"))[1]["configs"]]
        assert "channel-telegram-2" not in ids
        assert await pool.fetchval("SELECT count(*) FROM channel_secrets WHERE channel_id = 'channel-telegram-2'") == 0
        assert (await _call("DELETE", "channel-telegram-2"))[0] == 404
        assert (await _manager_call("managers.delete", {"manager_id": manager}))[0] == 200

    run_with_channel_configs(test, monkeypatch)


def test_two_adds_of_one_token_at_once_make_one_bot(monkeypatch: pytest.MonkeyPatch) -> None:
    async def test(pool: Any) -> None:
        manager, other = await _manager(pool), await _manager(pool, "other")
        results = await asyncio.gather(_telegram(manager), _telegram(other))
        assert sorted(status for status, _ in results) == [200, 409]
        assert await pool.fetchval("SELECT count(*) FROM channel_configs WHERE channel_type = 'telegram'") == 2

    run_with_channel_configs(test, monkeypatch)


def test_no_log_line_carries_a_token(monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
                                     lark_cli_stand_in: Path, platform: Any) -> None:  # noqa: F811
    from looper.agents import log_redaction

    caplog.set_level(logging.DEBUG)

    async def test(pool: Any) -> None:
        manager = await _manager(pool)
        await _telegram(manager)
        await _telegram(manager)
        await _call("PUT", "channel-telegram-2", {"bot_token": OTHER_TOKEN})
        await _call("POST", "channel_configs", {
            "channel_type": "lark_bot", "manager_id": manager, "config": {"app_id": "cli_third"},
            "bot_token": SECRET})
        await _call("DELETE", "channel-telegram-2")

    run_with_channel_configs(test, monkeypatch)
    formatter = log_redaction.RedactingFormatter("%(message)s")
    for record in caplog.records:
        text = formatter.format(record)
        for secret in (TOKEN, OTHER_TOKEN, SECRET):
            assert secret not in text
