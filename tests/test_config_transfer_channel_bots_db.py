"""Export and import carry only the built-in channel rows; the bots of Managers stay out (PostgreSQL)."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from tests.test_catalog_db import DSN
from tests.test_config_transfer_db import _installation, _looper, _plan, _rows

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


@pytest.mark.parametrize("secrets", [False, True])
def test_export_leaves_the_added_bot_out(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, secrets: bool) -> None:
    async def _test() -> None:
        async with _installation() as (pool, url):
            from looper.agents.serve import seed_channel_configs

            await seed_channel_configs(pool)
            await _add_bot(pool)
            path = tmp_path / "out.json"
            args = ["config", "export", str(path)] + (["--include-secrets"] if secrets else [])
            code, out, err = await _looper(monkeypatch, url, *args)
            assert code == 0, err
            text = path.read_text()
            channels = json.loads(text)["channels"]
            assert sorted(c["channel_id"] for c in channels) == ["channel-lark", "channel-lark_bot", "channel-telegram"]
            assert BOT not in text and BOT_TOKEN not in text and BOT_ALLOWED not in text
            assert "1 bots of Managers are not exported (add them again in Settings → Channels)" in out

    asyncio.run(_test())


def test_import_leaves_the_added_bot_untouched(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    async def _test() -> None:
        async with _installation() as (pool, url):
            from looper.agents.serve import seed_channel_configs

            await seed_channel_configs(pool)
            await _add_bot(pool)
            path = tmp_path / "out.json"
            assert (await _looper(monkeypatch, url, "config", "export", str(path)))[0] == 0
            data = json.loads(path.read_text())
            telegram = next(c for c in data["channels"] if c["channel_type"] == "telegram")
            telegram["display_name"] = "Imported name"
            path.write_text(json.dumps(data))
            before = await _bot_state(pool)
            code, _, err = await _looper(monkeypatch, url, "config", "import", str(path), "--apply", "--json")
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
            code, out, err = await _looper(monkeypatch, url, "config", "import", str(path), "--apply")
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
            code, out, err = await _looper(monkeypatch, url, "config", "import", str(path), "--apply", "--json")
            assert code == 0, err
            assert await pool.fetchval("SELECT notify_channel FROM schedules WHERE name = 'good'") is None
            assert any(f"telegram chat '{BOT_ALLOWED}' is not in this machine's sender allowlist" in n
                       for n in _plan(out)["notes"]), _plan(out)["notes"]

    asyncio.run(_test())
