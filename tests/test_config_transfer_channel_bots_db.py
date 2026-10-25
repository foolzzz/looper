"""Export and import carry only the built-in channel rows; the bots of Managers stay out (PostgreSQL)."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from looper.agents.bus import MessageBus
from looper.agents.ops.clients import ClientOps
from looper.agents.ops.service import Platform
from tests.db_support import DSN, run_db
from tests.test_api_v1_auth import TOKEN, _bearer, _send, _serving
from tests.test_config_transfer_db import _installation, _plan, _rows, _transfer_file

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")

BOT = "channel-telegram-2"
BOT_TOKEN = "123456:bot-token-of-the-added-bot"
BOT_ALLOWED = "555000111"


async def _add_bot(pool: Any) -> None:
    await pool.execute("INSERT INTO managers (id, name, system_prompt, enabled) VALUES ('manager-review', "
                       "'Review', 'Be.', true)")
    await pool.execute(
        "INSERT INTO channel_configs (channel_id, channel_type, display_name, enabled, config, manager_id) "
        "VALUES ($1, 'telegram', 'Review bot', false, $2, 'manager-review')",
        BOT, json.dumps({"allowed_user_ids": [BOT_ALLOWED]}))
    await pool.execute("INSERT INTO channel_secrets (channel_id, bot_token) VALUES ($1, $2)", BOT, BOT_TOKEN)


async def _bot_state(pool: Any) -> tuple[Any, ...]:
    row = await pool.fetchrow("SELECT c.*, s.bot_token FROM channel_configs c LEFT JOIN channel_secrets s "
                              "USING (channel_id) WHERE c.channel_id = $1", BOT)
    return tuple(tuple(row.items()))


async def _dump(pool: Any) -> dict[str, list[str]]:
    return {t: sorted(repr(tuple(r)) for r in await pool.fetch(f"SELECT * FROM {t}"))
            for t in ("channel_configs", "projects", "role_configs")}


@pytest.mark.parametrize("apply", [False, True])
@pytest.mark.parametrize("kind,unavailable", [
    ("lark", "candidate"), ("lark", "existing"), ("lark", "empty"), ("lark", "schema"),
    ("lark_bot", "existing"),
])
def test_lark_import_refuses_unknown_identity_without_partial_writes(
    monkeypatch: pytest.MonkeyPatch, apply: bool, kind: str, unavailable: str,
) -> None:
    from looper.agents.channels import lark_cli

    broken, duplicate = True, True
    calls: list[str | None] = []
    secret = "synthetic-private-identity-secret"

    async def identity(profile: str | None = None, *, fresh: bool = False) -> Any:
        calls.append(profile)
        assert fresh
        if broken and (profile == unavailable or unavailable == "empty" and profile == "candidate"):
            if unavailable == "empty":
                return lark_cli.Identity(profile, "", "Bot", "ou_bot")
            raise lark_cli.LarkCliError(f"{secret} at /private/profile")
        app = "cli_shared" if duplicate and profile in ("candidate", "existing") else f"cli_{profile}"
        return lark_cli.Identity(profile or "", app, "Bot", "ou_bot")

    monkeypatch.setattr(lark_cli, "identity", identity)

    async def test(pool: Any) -> None:
        from looper.agents.config_transfer import run_import
        from looper.db.channel_configs import create_bot

        await _add_bot(pool)
        async with pool.acquire() as conn:
            await create_bot("lark", "manager-review", display_name="Existing", enabled=False,
                             config={"profile": "existing"}, secret=None, conn=conn)
        config = {"profile": "candidate"} if kind == "lark" else {"app_id": "cli_shared"}
        if unavailable == "schema":
            config["allowed_open_ids"] = ["bad"]
        document = {"format": "looper-config/v3", "channels": [
            {"channel_type": kind, "display_name": "Imported", "enabled": True, "config": config}]}
        before, bot = await _dump(pool), await _bot_state(pool)
        async with pool.acquire() as conn:
            plan = await run_import(conn, document, apply=apply)
        change = next(c for c in plan.view()["changes"] if c["section"] == "channels")
        assert change["action"] == "skipped", change
        if unavailable == "schema":
            assert not calls
        else:
            assert "lark-cli" in change["reason"] and "retry" in change["reason"]
        assert secret not in json.dumps(plan.view()) and "/private/profile" not in json.dumps(plan.view())
        assert await _dump(pool) == before and await _bot_state(pool) == bot
        if unavailable != "schema":
            nonlocal broken, duplicate
            broken = False
            async with pool.acquire() as conn:
                conflict = await run_import(conn, document, apply=apply)
            assert conflict.view()["counts"]["channels"]["skipped"] == 1
            assert await _dump(pool) == before and await _bot_state(pool) == bot
            duplicate = False
            async with pool.acquire() as conn:
                recovered = await run_import(conn, document, apply=apply)
            assert recovered.view()["counts"]["channels"]["changed"] == 1
            field = "profile" if kind == "lark" else "app_id"
            saved = await pool.fetchval("SELECT config->>$1 FROM channel_configs WHERE channel_id = $2",
                                        field, f"channel-{kind}")
            assert saved == (config[field] if apply else "")

    from looper.agents.serve import seed_channel_configs

    run_db(test, monkeypatch, setup=seed_channel_configs, timeout=30)


def test_preview_without_channel_schema_still_requires_lark_identity(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    from unittest.mock import AsyncMock
    from looper.agents.channels import lark_cli

    monkeypatch.setattr(lark_cli, "identity", AsyncMock(side_effect=lark_cli.LarkCliError("unavailable")))

    async def test() -> None:
        async with _installation(schema=False) as (pool, url):
            path = tmp_path / "in.json"
            path.write_text(json.dumps({"format": "looper-config/v3", "channels": [
                {"channel_type": "lark", "config": {"profile": "candidate"}}]}))
            code, out, err = await _transfer_file(url, "import", str(path), json_output=True)
            assert code == 0, err
            assert _plan(out)["counts"]["channels"]["skipped"] == 1
            assert await pool.fetchval("SELECT to_regclass('channel_configs')") is None

    asyncio.run(asyncio.wait_for(test(), timeout=30))


@pytest.mark.parametrize("command", ["export", "import"])
def test_export_and_preview_read_channels_before_the_manager_migration(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, command: str,
) -> None:
    # The retired offline CLI is gone; exercise the shared implementation through v1 without running migrations.
    async def _test(pool: Any) -> None:
        from looper.agents.serve import seed_channel_configs

        await seed_channel_configs(pool)
        await pool.execute("ALTER TABLE channel_configs DROP COLUMN manager_id CASCADE")
        await pool.execute("UPDATE channel_configs SET display_name = 'Saved Telegram' "
                           "WHERE channel_id = 'channel-telegram'")
        before, rows = await _dump(pool), await _rows(pool)
        document = {"format": "looper-config/v3", "channels": [
            {"channel_type": "telegram", "channel_id": "channel-telegram", "display_name": "Preview name",
             "enabled": False, "config": {}}]}
        async with _serving(tmp_path) as server:
            server._client_ops = ClientOps(Platform(bus=MessageBus(runtime_root=tmp_path),
                                                   task_router=server._task_router, runtime_root=tmp_path))
            try:
                operation = "export" if command == "export" else "import_preview"
                status, _, answer = await _send(server, f"POST /api/v1/config/{operation}", _bearer(TOKEN),
                                                json.dumps({} if command == "export" else {"document": document}))
                assert status == 200, answer
                assert answer["status"] == "ok", answer
                if command == "export":
                    channels = answer["result"]["document"]["channels"]
                    assert next(c for c in channels if c["channel_id"] == "channel-telegram")["display_name"] == "Saved Telegram"
                else:
                    plan = answer["result"]["plan"]
                    assert not plan["applied"]
                    assert plan["counts"]["channels"]["changed"] == 1
                assert await _dump(pool) == before and await _rows(pool) == rows
            finally:
                server._config_imports.clear()

    run_db(_test, monkeypatch, timeout=30)


@pytest.mark.parametrize("secrets", [False, True])
def test_export_leaves_the_added_bot_out(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, secrets: bool) -> None:
    async def _test() -> None:
        async with _installation() as (pool, url):
            from looper.agents.serve import seed_channel_configs

            await seed_channel_configs(pool)
            await _add_bot(pool)
            path = tmp_path / "out.json"
            code, out, err = await _transfer_file(url, "export", str(path), include_secrets=secrets)
            assert code == 0, err
            text = path.read_text()
            channels = json.loads(text)["channels"]
            assert sorted(c["channel_id"] for c in channels) == ["channel-lark", "channel-lark_bot", "channel-telegram"]
            assert BOT not in text and BOT_TOKEN not in text and BOT_ALLOWED not in text
            assert "1 bots of Managers are not exported (add them again in Settings → Channels)" in out

    asyncio.run(_test())


@pytest.mark.parametrize("policy", [False, True])
def test_channel_import_cannot_set_or_erase_bound_only_policy(monkeypatch: pytest.MonkeyPatch, policy: bool) -> None:
    from looper.agents.config_transfer import run_import
    from looper.agents.serve import seed_channel_configs
    from looper.db import chat_bindings

    async def test(pool: Any) -> None:
        async with pool.acquire() as conn, conn.transaction():
            await chat_bindings.set_bound_chats_only("channel-telegram", policy, conn=conn)
        document = {"format": "looper-config/v3", "channels": [{
            "channel_type": "telegram", "channel_id": "channel-telegram", "display_name": "Imported bot",
            "enabled": False, "config": {}, "bound_chats_only": not policy}]}
        async with pool.acquire() as conn:
            plan = await run_import(conn, document, apply=True)
        assert plan.view()["counts"]["channels"]["changed"] == 1
        row = await pool.fetchrow("SELECT display_name, bound_chats_only FROM channel_configs "
                                  "WHERE channel_id = 'channel-telegram'")
        assert row["display_name"] == "Imported bot" and row["bound_chats_only"] is policy

    run_db(test, monkeypatch, setup=seed_channel_configs, timeout=30)


def test_import_leaves_the_added_bot_untouched(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    async def _test() -> None:
        async with _installation() as (pool, url):
            from looper.agents.serve import seed_channel_configs

            await seed_channel_configs(pool)
            await _add_bot(pool)
            path = tmp_path / "out.json"
            assert (await _transfer_file(url, "export", str(path)))[0] == 0
            data = json.loads(path.read_text())
            telegram = next(c for c in data["channels"] if c["channel_type"] == "telegram")
            telegram["display_name"] = "Imported name"
            path.write_text(json.dumps(data))
            before = await _bot_state(pool)
            code, _, err = await _transfer_file(url, "import", str(path), apply=True, json_output=True)
            assert code == 0, err
            assert await _bot_state(pool) == before
            assert await pool.fetchval("SELECT display_name FROM channel_configs WHERE channel_id = "
                                       "'channel-telegram'") == "Imported name"

    asyncio.run(_test())


@pytest.mark.parametrize("entry", [
    {"channel_type": "telegram", "channel_id": BOT, "display_name": "x", "enabled": False, "config": {}},
    {"channel_type": "telegram", "channel_id": "channel-telegram", "manager_id": "manager-review",
     "display_name": "x", "enabled": False, "config": {}},
])
def test_a_file_with_a_bot_is_refused_and_nothing_changes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, entry: dict[str, Any],
) -> None:
    async def _test() -> None:
        async with _installation() as (pool, url):
            from looper.agents.serve import seed_channel_configs

            await seed_channel_configs(pool)
            await _add_bot(pool)
            path = tmp_path / "in.json"
            path.write_text(json.dumps({"format": "looper-config/v3", "settings": {"roles": [
                {"role": "worker", "prompt": "changed", "timeout_seconds": 60, "enabled": True}]},
                "channels": [entry]}))
            before, rows = await _dump(pool), await _rows(pool)
            code, out, err = await _transfer_file(url, "import", str(path), apply=True)
            assert code != 0
            assert "is a bot of a Manager" in out + err
            assert await _dump(pool) == before and await _rows(pool) == rows

    asyncio.run(_test())


def test_a_schedule_cannot_tell_a_chat_only_the_added_bot_allows(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    async def _test() -> None:
        async with _installation() as (pool, url):
            from looper.agents.serve import seed_channel_configs

            await seed_channel_configs(pool)
            await _add_bot(pool)
            path = tmp_path / "in.json"
            schedule = {"name": "good", "cron": "0 3 * * *", "timezone": "UTC", "requirement": "r",
                        "notify": "always", "notify_target": {"channel": "telegram", "chat": BOT_ALLOWED}}
            path.write_text(json.dumps({"format": "looper-config/v2", "projects": [
                {"name": "calc", "schedules": [schedule]}]}))
            code, out, err = await _transfer_file(url, "import", str(path), apply=True, json_output=True)
            assert code == 0, err
            assert await pool.fetchval("SELECT notify_channel FROM schedules WHERE name = 'good'") is None
            assert any(f"telegram chat '{BOT_ALLOWED}' is not in this machine's sender allowlist" in n
                       for n in _plan(out)["notes"]), _plan(out)["notes"]

    asyncio.run(_test())


@pytest.mark.parametrize("apply", [False, True])
@pytest.mark.parametrize("unset_env", [False, True])
def test_the_built_in_channel_is_not_imported_as_an_added_bot(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, apply: bool, unset_env: bool,
) -> None:
    # The built-in Telegram row stores no token: a bot_token_env naming the added bot's token would make it that bot,
    # and the server would then skip the added row and answer its chats as the default Manager. So would a
    # bot_token_env naming an unset variable while LOOPER_TELEGRAM_BOT_TOKEN, its last fallback, holds the added bot.
    if unset_env:
        monkeypatch.setenv("LOOPER_TELEGRAM_BOT_TOKEN", "123456:another-secret-of-the-added-bot")
        monkeypatch.delenv("ALT_TELEGRAM_TOKEN", raising=False)
    else:
        monkeypatch.setenv("LOOPER_TELEGRAM_BOT_TOKEN", "999999:the-default-bot")
        monkeypatch.setenv("ALT_TELEGRAM_TOKEN", "123456:another-secret-of-the-added-bot")

    async def _test() -> None:
        async with _installation() as (pool, url):
            from looper.agents.serve import seed_channel_configs

            await seed_channel_configs(pool)
            await _add_bot(pool)
            before = await _dump(pool)
            path = tmp_path / "in.json"
            path.write_text(json.dumps({"format": "looper-config/v3", "channels": [
                {"channel_type": "telegram", "channel_id": "channel-telegram", "display_name": "Telegram",
                 "enabled": True, "config": {"allowed_user_ids": ["1"], "bot_token_env": "ALT_TELEGRAM_TOKEN"}}]}))
            code, out, err = await _transfer_file(url, "import", str(path), apply=apply, json_output=True)
            assert code == 0, err
            change = next(c for c in _plan(out)["changes"] if c["section"] == "channels")
            assert change["action"] == "skipped" and f"({BOT})" in change["reason"], change
            assert "another-secret" not in out + err and BOT_TOKEN not in out + err
            assert await _dump(pool) == before

    asyncio.run(_test())


def test_both_built_in_lark_channels_of_one_app_are_imported(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, lark_cli_stand_in: Path,
) -> None:
    # The built-in lark-cli channel acts as lark-cli's active profile, of app cli_main; the built-in bot token
    # channel of cli_main runs beside it, as today, so the import does not skip it as the same bot.
    async def _test() -> None:
        async with _installation() as (pool, url):
            from looper.agents.serve import seed_channel_configs

            await seed_channel_configs(pool)
            await pool.execute("UPDATE channel_configs SET enabled = true WHERE channel_id = 'channel-lark'")
            path = tmp_path / "in.json"
            path.write_text(json.dumps({"format": "looper-config/v3", "channels": [
                {"channel_type": "lark_bot", "channel_id": "channel-lark_bot", "display_name": "Lark bot",
                 "enabled": False, "config": {"app_id": "cli_main"}}]}))
            code, out, err = await _transfer_file(url, "import", str(path), apply=True, json_output=True)
            assert code == 0, err
            change = next(c for c in _plan(out)["changes"] if c["section"] == "channels")
            assert change["action"] != "skipped", change
            assert await pool.fetchval("SELECT config->>'app_id' FROM channel_configs "
                                       "WHERE channel_id = 'channel-lark_bot'") == "cli_main"

    asyncio.run(_test())
