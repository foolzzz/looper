"""serve starts a runner for every enabled bot (RFC §9.1), on an isolated PostgreSQL schema."""

from __future__ import annotations

import logging
import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from looper.agents.channels.bots import bot_key, builtin_env_token, clashes
from tests.test_channel_configs_db import _run
from tests.test_lark_bot_settings_db import platform  # noqa: F401 (a fixture)

DSN = os.environ.get("LOOPER_TEST_DATABASE_URL")
requires_db = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")

OPEN_ID = "ou_allowed000001"
OTHER = "ou_other00000001"
TOKEN_1 = "111:AAfirstsecretvalue"
TOKEN_2 = "222:AAsecondsecretvalue"
LARK_SECRET = "synthetic-app-secret-value"


# --- bot_key / clashes ---

def test_bot_key_cases(monkeypatch: pytest.MonkeyPatch) -> None:
    assert bot_key("telegram", {}, "123:abc") == "telegram:123"
    assert bot_key("telegram", {}, "abc") is None
    assert bot_key("telegram", {}, None) is None
    assert "abc" not in (bot_key("telegram", {}, "123:abc") or "")
    monkeypatch.setenv("LOOPER_TELEGRAM_BOT_TOKEN", "555:x")
    assert bot_key("telegram", {}, None, env_token=builtin_env_token({})) == "telegram:555"
    assert bot_key("lark", {"profile": ""}, None) == "lark:"
    assert bot_key("lark", {}, None) == "lark:"
    assert bot_key("lark", {"profile": "p1"}, None) == "lark:p1"
    assert bot_key("lark_bot", {"app_id": "cli_a"}, "s") == "lark_bot:cli_a"
    assert bot_key("lark_bot", {"app_id": ""}, "s") is None


def test_clashes_names_later_rows() -> None:
    rows = [{"channel_id": f"channel-telegram{suffix}", "channel_type": "telegram", "config": {}, "secret": secret}
            for suffix, secret in (("", "123:a"), ("-2", "123:b"), ("-3", "124:c"), ("-4", None))]
    assert clashes(rows) == {"channel-telegram-2": "channel-telegram"}


# --- serve's channel pass ---

class _Registry:
    def __init__(self) -> None:
        self.adapters: list[Any] = []

    async def register(self, adapter: Any) -> None:
        self.adapters.append(adapter)

    async def unregister(self, channel_id: str) -> None:
        pass


async def _bot(pool: Any, channel_type: str, config: dict[str, Any], secret: str | None, *,
               manager_id: str = "manager") -> str:
    from looper.db.channel_configs import create_bot

    async with pool.acquire() as conn:
        row = await create_bot(channel_type, manager_id, display_name="bot", enabled=True, config=config,
                               secret=secret, conn=conn)
    return row["channel_id"]


async def _builtin(pool: Any, channel_id: str, config: dict[str, Any], secret: str | None = None) -> None:
    from looper.db.channel_configs import get_config, set_bot_token, set_config

    current = (await get_config(channel_id, pool=pool))["config"]
    await set_config(channel_id, {"enabled": True, "config": {**current, **config}}, pool=pool)
    if secret:
        await set_bot_token(channel_id, secret, pool=pool)


async def _pass(types: tuple[str, ...], tmp_path: Path, running: Any = lambda manager_id: True) -> list[Any]:
    from looper.agents import serve

    registry = _Registry()
    rows = await serve._enabled_bot_rows()
    await serve._register_bots(registry, rows, tmp_path, running, types)  # type: ignore[arg-type]
    return registry.adapters


@requires_db
def test_added_telegram_bot_runs_beside_the_builtin_one(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    async def test(pool: Any) -> None:
        await pool.execute("INSERT INTO managers (id, name, system_prompt, enabled) "
                           "VALUES ('manager-review', 'Review', 'x', true)")
        await _builtin(pool, "channel-telegram", {"allowed_user_ids": ["7"]}, TOKEN_1)
        added = await _bot(pool, "telegram", {"allowed_user_ids": ["8"]}, TOKEN_2, manager_id="manager-review")
        adapters = await _pass(("telegram",), tmp_path)
        assert [a.channel_id for a in adapters] == ["channel-telegram", added]
        assert adapters[0]._manager_id == "manager" and adapters[0]._bot_token == TOKEN_1
        assert adapters[1]._manager_id == "manager-review" and adapters[1]._bot_token == TOKEN_2

    _run(test, monkeypatch)


@requires_db
def test_only_builtin_rows_register_todays_ids(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    async def test(pool: Any) -> None:
        await _builtin(pool, "channel-telegram", {"allowed_user_ids": ["7"]}, TOKEN_1)
        await _builtin(pool, "channel-lark", {"allowed_open_ids": [OPEN_ID]})
        from looper.agents.channels import lark_cli

        async def check(config: Any, *, fresh: bool = False) -> Any:
            return None, SimpleNamespace(profile="", app_id="cli_x", bot_name="B", bot_open_id="ou_bot00000001")

        monkeypatch.setattr(lark_cli, "check", check)
        adapters = [*await _pass(("telegram",), tmp_path), *await _pass(("lark", "lark_bot"), tmp_path)]
        assert [a.channel_id for a in adapters] == ["channel-telegram", "channel-lark"]

    _run(test, monkeypatch)


@requires_db
def test_added_bot_without_a_token_ignores_the_env_token(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("LOOPER_TELEGRAM_BOT_TOKEN", "555:envsecretvalue")

    async def test(pool: Any) -> None:
        await _builtin(pool, "channel-telegram", {"allowed_user_ids": ["7"]})
        added = await _bot(pool, "telegram", {"allowed_user_ids": ["8"]}, None)
        adapters = await _pass(("telegram",), tmp_path)
        assert [a.channel_id for a in adapters] == ["channel-telegram", added]
        assert adapters[0]._bot_token == "555:envsecretvalue"
        assert adapters[1]._bot_token == ""

    _run(test, monkeypatch)


@requires_db
def test_same_telegram_bot_twice_starts_once(monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
                                             caplog: pytest.LogCaptureFixture) -> None:
    async def test(pool: Any) -> None:
        await _builtin(pool, "channel-telegram", {"allowed_user_ids": ["7"]}, TOKEN_1)
        added = await _bot(pool, "telegram", {"allowed_user_ids": ["8"]}, "111:AAanothersecretvalue")
        with caplog.at_level(logging.INFO):
            adapters = await _pass(("telegram",), tmp_path)
        assert [a.channel_id for a in adapters] == ["channel-telegram"]
        errors = [r.getMessage() for r in caplog.records if r.levelno >= logging.ERROR]
        assert errors == [f"{added} not started: it is the same bot as channel-telegram"]
        assert "AAfirstsecretvalue" not in caplog.text and "AAanothersecretvalue" not in caplog.text

    _run(test, monkeypatch)


@requires_db
def test_same_lark_profile_twice_starts_once(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from looper.agents.channels import lark_cli

    async def check(config: Any, *, fresh: bool = False) -> Any:
        return None, SimpleNamespace(profile=config.get("profile"), app_id="cli_x", bot_name="B",
                                     bot_open_id="ou_bot00000001")

    monkeypatch.setattr(lark_cli, "check", check)

    async def test(pool: Any) -> None:
        await _builtin(pool, "channel-lark", {"profile": "p1", "allowed_open_ids": [OPEN_ID]})
        await _bot(pool, "lark", {"profile": "p1", "allowed_open_ids": [OPEN_ID]}, None)
        assert [a.channel_id for a in await _pass(("lark",), tmp_path)] == ["channel-lark"]

    _run(test, monkeypatch)


@requires_db
def test_one_lark_app_under_any_name_starts_once(monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
                                                 lark_cli_stand_in: Path, caplog: pytest.LogCaptureFixture) -> None:
    # lark-cli's active profile is cli_main, of app cli_main.
    from looper.agents import serve

    async def test(pool: Any) -> None:
        await _builtin(pool, "channel-lark", {"allowed_open_ids": [OPEN_ID]})
        by_name = await _bot(pool, "lark", {"profile": "cli_main", "allowed_open_ids": [OPEN_ID]}, None)
        by_token = await _bot(pool, "lark_bot", {"app_id": "cli_main", "allowed_open_ids": [OPEN_ID]}, LARK_SECRET)
        other = await _bot(pool, "lark_bot", {"app_id": "cli_other", "allowed_open_ids": [OPEN_ID]}, LARK_SECRET)
        with caplog.at_level(logging.ERROR):
            rows = await serve._enabled_bot_rows()
        assert [r["channel_id"] for r in rows if r["channel_type"] != "telegram"] == ["channel-lark", other]
        errors = sorted(r.getMessage() for r in caplog.records if r.levelno >= logging.ERROR)
        assert errors == sorted(f"{later} not started: it is the same bot as channel-lark"
                                for later in (by_name, by_token))

    _run(test, monkeypatch)


@requires_db
def test_lark_app_learnt_late_still_starts_once(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, platform: Any,
                                               caplog: pytest.LogCaptureFixture) -> None:
    # lark-cli cannot say which app a profile is when the rows are read, and can when each adapter starts: the
    # rows of one app under other names still start once.
    from looper.agents.channels import lark_cli

    async def identity(profile: Any = None, *, fresh: bool = False) -> Any:
        raise lark_cli.LarkCliError("not signed in yet")

    async def check(config: Any, *, fresh: bool = False) -> Any:
        return None, SimpleNamespace(profile=config.get("profile"), app_id="cli_x", bot_name="B",
                                     bot_open_id="ou_bot00000001")

    monkeypatch.setattr(lark_cli, "identity", identity)
    monkeypatch.setattr(lark_cli, "check", check)

    async def test(pool: Any) -> None:
        await pool.execute("INSERT INTO managers (id, name, system_prompt, enabled) "
                           "VALUES ('manager-review', 'Review', 'x', true)")
        await _builtin(pool, "channel-lark", {"allowed_open_ids": [OPEN_ID]})
        by_profile = await _bot(pool, "lark", {"profile": "p2", "allowed_open_ids": [OPEN_ID]}, None,
                                manager_id="manager-review")
        by_token = await _bot(pool, "lark_bot", {"app_id": "cli_x", "allowed_open_ids": [OPEN_ID]}, LARK_SECRET,
                              manager_id="manager-review")
        with caplog.at_level(logging.ERROR):
            adapters = await _pass(("lark", "lark_bot"), tmp_path)
        for adapter in adapters:
            if adapter.channel_id == by_token:
                await adapter._transport.aclose()
        assert [a.channel_id for a in adapters] == ["channel-lark"]
        errors = sorted(r.getMessage() for r in caplog.records if r.levelno >= logging.ERROR)
        assert errors == sorted(f"{later} not started: it is the same bot as channel-lark"
                                for later in (by_profile, by_token))

    _run(test, monkeypatch)


@requires_db
def test_added_lark_cli_bot_records_refused_senders_under_its_id(monkeypatch: pytest.MonkeyPatch,
                                                                 tmp_path: Path) -> None:
    from looper.agents.channels import lark_cli
    from looper.db import channel_configs

    async def check(config: Any, *, fresh: bool = False) -> Any:
        return None, SimpleNamespace(profile=config.get("profile"), app_id="cli_x", bot_name="B",
                                     bot_open_id="ou_bot00000001")

    recorded: list[tuple[str, str, str | None]] = []

    async def record_rejected(channel_id: str, sender_id: str, *, chat_id: str | None = None) -> None:
        recorded.append((channel_id, sender_id, chat_id))

    monkeypatch.setattr(lark_cli, "check", check)
    monkeypatch.setattr(channel_configs, "record_rejected", record_rejected)

    async def test(pool: Any) -> None:
        added = await _bot(pool, "lark", {"profile": "p2", "allowed_open_ids": [OPEN_ID]}, None)
        assert added.startswith("channel-lark-")
        (adapter,) = await _pass(("lark",), tmp_path)
        assert adapter.channel_id == added
        await adapter._on_rejected(OTHER, "oc_team")
        assert recorded == [(added, OTHER, "oc_team")]

    _run(test, monkeypatch)


@requires_db
def test_lark_bot_secret_is_masked_in_logs(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, platform: Any) -> None:
    from looper.agents import log_redaction

    monkeypatch.setattr(log_redaction, "_secrets", ())

    async def test(pool: Any) -> None:
        added = await _bot(pool, "lark_bot", {"app_id": "cli_main", "domain": "feishu",
                                              "allowed_open_ids": [OPEN_ID]}, LARK_SECRET)
        (adapter,) = await _pass(("lark_bot",), tmp_path)
        assert adapter.channel_id == added
        await adapter._transport.aclose()
        record = logging.LogRecord("t", logging.INFO, __file__, 1, "leak %s", (LARK_SECRET,), None)
        line = log_redaction.RedactingFormatter("%(message)s").format(record)
        assert LARK_SECRET not in line and "leak" in line

    _run(test, monkeypatch)


def test_manager_running_answers_true_until_the_fleet_exists() -> None:
    # The same expression serve builds around ``fleet_box``.
    fleet_box: list[Any] = []

    def manager_running(manager_id: str) -> bool:
        return fleet_box[0].is_running(manager_id) if fleet_box else True

    assert manager_running("manager-review") is True
    fleet_box.append(SimpleNamespace(is_running=lambda manager_id: manager_id == "manager"))
    assert manager_running("manager") is True and manager_running("manager-review") is False


@requires_db
def test_database_facts_see_an_added_lark_row(monkeypatch: pytest.MonkeyPatch) -> None:
    from looper.agents import self_check

    async def test(pool: Any) -> None:
        assert await _facts(pool) is False
        await _bot(pool, "lark", {"profile": "p2", "allowed_open_ids": [OPEN_ID]}, None)
        assert await _facts(pool) is True

    async def _facts(pool: Any) -> bool:
        schema = await pool.fetchval("SELECT current_schema()")
        return (await self_check._database_facts(f"{DSN}{'&' if '?' in DSN else '?'}search_path={schema}")
                ).lark_enabled

    _run(test, monkeypatch)
