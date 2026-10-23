"""serve starts a runner for every enabled bot (RFC §9.1), on an isolated PostgreSQL schema."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from looper.agents.channels.bots import bot_key, builtin_env_token, clashes
from tests.db_support import DSN, run_with_channel_configs
from tests.test_lark_bot_settings_db import platform  # noqa: F401 (a fixture)

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


def test_builtin_env_token_falls_back_as_main_does(monkeypatch: pytest.MonkeyPatch) -> None:
    # env[bot_token_env], then LOOPER_TELEGRAM_BOT_TOKEN (the adapter's own last fallback on main).
    monkeypatch.setenv("LOOPER_TELEGRAM_BOT_TOKEN", "555:std")
    monkeypatch.delenv("UNSET_TELEGRAM_TOKEN", raising=False)
    monkeypatch.setenv("ALT_TELEGRAM_TOKEN", "666:alt")
    assert builtin_env_token({}) == "555:std"
    assert builtin_env_token({"bot_token_env": "ALT_TELEGRAM_TOKEN"}) == "666:alt"
    assert builtin_env_token({"bot_token_env": "UNSET_TELEGRAM_TOKEN"}) == "555:std"
    monkeypatch.setenv("ALT_TELEGRAM_TOKEN", "")
    assert builtin_env_token({"bot_token_env": "ALT_TELEGRAM_TOKEN"}) == "555:std"


def test_clashes_names_later_rows() -> None:
    rows = [{"channel_id": f"channel-telegram{suffix}", "channel_type": "telegram", "config": {}, "secret": secret}
            for suffix, secret in (("", "123:a"), ("-2", "123:b"), ("-3", "124:c"), ("-4", None))]
    assert clashes(rows) == {"channel-telegram-2": "channel-telegram"}


def test_the_built_in_lark_rows_of_one_app_both_run_and_an_added_bot_of_it_does_not() -> None:
    def row(channel_id: str, channel_type: str) -> dict[str, Any]:
        config = {"app_id": "cli_a"} if channel_type == "lark_bot" else {"profile": "p1"}
        return {"channel_id": channel_id, "channel_type": channel_type, "config": config, "app_id": "cli_a"}

    builtin = [row("channel-lark", "lark"), row("channel-lark_bot", "lark_bot")]
    assert clashes(builtin) == {}
    assert clashes([*builtin, row("channel-lark_bot-2", "lark_bot"), row("channel-lark-2", "lark")]) == {
        "channel-lark_bot-2": "channel-lark", "channel-lark-2": "channel-lark"}
    assert clashes([row("channel-lark_bot", "lark_bot"), row("channel-lark-2", "lark")]) == {
        "channel-lark-2": "channel-lark_bot"}


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

    run_with_channel_configs(test, monkeypatch)


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

    run_with_channel_configs(test, monkeypatch)


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

    run_with_channel_configs(test, monkeypatch)


@requires_db
def test_builtin_row_whose_bot_token_env_is_unset_starts_with_the_standard_token(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    # The reviewer's upgrade: an old built-in row's bot_token_env names an unset variable; main ran the bot in
    # LOOPER_TELEGRAM_BOT_TOKEN, and an added bot of that bot id is the same bot.
    monkeypatch.setenv("LOOPER_TELEGRAM_BOT_TOKEN", TOKEN_1)
    monkeypatch.delenv("UNSET_TELEGRAM_TOKEN", raising=False)

    async def test(pool: Any) -> None:
        await _builtin(pool, "channel-telegram", {"allowed_user_ids": ["7"], "bot_token_env": "UNSET_TELEGRAM_TOKEN"})
        added = await _bot(pool, "telegram", {"allowed_user_ids": ["8"]}, "111:AAanothersecretvalue")
        with caplog.at_level(logging.INFO):
            adapters = await _pass(("telegram",), tmp_path)
        assert [a.channel_id for a in adapters] == ["channel-telegram"]
        assert adapters[0]._bot_token == TOKEN_1
        errors = [r.getMessage() for r in caplog.records if r.levelno >= logging.ERROR]
        assert errors == [f"{added} not started: it is the same bot as channel-telegram"]

    run_with_channel_configs(test, monkeypatch)


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

    run_with_channel_configs(test, monkeypatch)


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

    run_with_channel_configs(test, monkeypatch)


@requires_db
def test_one_lark_app_under_any_name_starts_once(monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
                                                 lark_cli_stand_in: Path, platform: Any,
                                                 caplog: pytest.LogCaptureFixture) -> None:
    # lark-cli's active profile is cli_main, of app cli_main.
    async def test(pool: Any) -> None:
        await _builtin(pool, "channel-lark", {"allowed_open_ids": [OPEN_ID]})
        by_name = await _bot(pool, "lark", {"profile": "cli_main", "allowed_open_ids": [OPEN_ID]}, None)
        by_token = await _bot(pool, "lark_bot", {"app_id": "cli_main", "allowed_open_ids": [OPEN_ID]}, LARK_SECRET)
        other = await _bot(pool, "lark_bot", {"app_id": "cli_other", "allowed_open_ids": [OPEN_ID]}, LARK_SECRET)
        with caplog.at_level(logging.ERROR):
            adapters = await _pass(("lark", "lark_bot"), tmp_path)
        for adapter in adapters:
            if adapter.channel_id == other:
                await adapter._transport.aclose()
        assert [a.channel_id for a in adapters] == ["channel-lark", other]
        errors = sorted(r.getMessage() for r in caplog.records if r.levelno >= logging.ERROR)
        assert errors == sorted(f"{later} not started: it is the same bot as channel-lark"
                                for later in (by_name, by_token))

    run_with_channel_configs(test, monkeypatch)


@requires_db
def test_both_built_in_lark_rows_of_one_app_start(monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
                                                  lark_cli_stand_in: Path, platform: Any,  # noqa: F811
                                                  caplog: pytest.LogCaptureFixture) -> None:
    # As today: the lark-cli row (active profile cli_main, of app cli_main) and the bot token row of cli_main both
    # run; RFC §9.1 keeps the same-bot check inside one channel type for them.
    async def test(pool: Any) -> None:
        await _builtin(pool, "channel-lark", {"allowed_open_ids": [OPEN_ID]})
        await _builtin(pool, "channel-lark_bot", {"app_id": "cli_main", "allowed_open_ids": [OPEN_ID]}, LARK_SECRET)
        with caplog.at_level(logging.ERROR):
            adapters = await _pass(("lark", "lark_bot"), tmp_path)
        for adapter in adapters:
            if adapter.channel_id == "channel-lark_bot":
                await adapter._transport.aclose()
        assert [a.channel_id for a in adapters] == ["channel-lark", "channel-lark_bot"]
        assert [r.getMessage() for r in caplog.records if r.levelno >= logging.ERROR] == []

    run_with_channel_configs(test, monkeypatch)


@requires_db
def test_builtin_lark_row_that_cannot_start_leaves_its_app_to_a_later_row(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path, lark_cli_stand_in: Path, platform: Any,
        caplog: pytest.LogCaptureFixture) -> None:
    # The reviewer's upgrade: the built-in lark-cli row names app cli_main but cannot start; the bot-token row of
    # cli_main, which ran before, still runs.
    from looper.agents.channels import lark_cli

    async def check(config: Any, *, fresh: bool = False) -> Any:
        return "lark-cli is not signed in as the bot", None

    monkeypatch.setattr(lark_cli, "check", check)

    async def test(pool: Any) -> None:
        await _builtin(pool, "channel-lark", {"allowed_open_ids": [OPEN_ID]})
        await _builtin(pool, "channel-lark_bot", {"app_id": "cli_main", "allowed_open_ids": [OPEN_ID]}, LARK_SECRET)
        with caplog.at_level(logging.ERROR):
            adapters = await _pass(("lark", "lark_bot"), tmp_path)
        for adapter in adapters:
            await adapter._transport.aclose()
        assert [a.channel_id for a in adapters] == ["channel-lark_bot"]
        errors = [r.getMessage() for r in caplog.records if r.levelno >= logging.ERROR]
        assert errors == ["channel-lark not started: lark-cli is not signed in as the bot"]

    run_with_channel_configs(test, monkeypatch)


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

    run_with_channel_configs(test, monkeypatch)


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

    run_with_channel_configs(test, monkeypatch)


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

    run_with_channel_configs(test, monkeypatch)


@requires_db
@pytest.mark.parametrize("channel_type", ["telegram", "lark"])
@pytest.mark.parametrize(("manager_id", "enabled"), [("manager", True), ("manager-review", False),
                                                     ("manager-review", True)])
def test_startup_bot_backlog_reaches_only_an_enabled_manager(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, channel_type: str, manager_id: str, enabled: bool,
) -> None:
    from unittest.mock import AsyncMock, MagicMock

    from looper import db, ops_config
    from looper.agents import managers, managers_api, operator_notes, profile_registry, serve
    from looper.agents.channels import lark_cli
    from looper.agents.channels.lark import LarkChannelAdapter
    from looper.agents.channels.telegram import TelegramChannelAdapter
    from looper.agents.runtimes import codex_login, grok_login
    from looper.agents.tiers.manager import TURNED_OFF_REPLY
    from tests.test_agent_channels import _lark_event, _tg_update
    from tests.test_channel_post import _Bot
    from tests.test_lark_transport import StubTransport

    class Started(Exception):
        pass

    async def test(pool: Any) -> None:
        if manager_id != "manager":
            await pool.execute("INSERT INTO managers (id, name, system_prompt, enabled) "
                               "VALUES ('manager-review', 'Review', 'x', $1)", enabled)
        config = {"allowed_user_ids": ["7"]} if channel_type == "telegram" else {"allowed_open_ids": [OPEN_ID]}
        await _bot(pool, channel_type, config, TOKEN_1 if channel_type == "telegram" else None,
                   manager_id=manager_id)
        adapters: list[Any] = []
        registries: list[Any] = []
        incoming: list[str] = []
        bot, transport = _Bot(), StubTransport()
        pending = ["sent while offline"]

        async def start_polling(*, drop_pending_updates: bool) -> None:
            assert drop_pending_updates is True
            pending.clear()

        if channel_type == "telegram":
            # Exercise the real adapter startup, including the destructive polling bootstrap.
            app = MagicMock()
            app.bot = bot
            bot.set_my_commands = AsyncMock()
            for name in ("initialize", "start", "stop", "shutdown"):
                setattr(app, name, AsyncMock())
            app.updater.start_polling = start_polling
            app.updater.stop = AsyncMock()
            monkeypatch.setattr("telegram.ext.Application.builder", lambda: MagicMock(
                token=lambda _: SimpleNamespace(build=lambda: app)))
        telegram_start = TelegramChannelAdapter.start

        async def receive(adapter: Any) -> None:
            text = f"backlog {len(incoming)}"
            incoming.append(text)
            if channel_type == "telegram":
                await adapter._on_message(_tg_update(7, text), None)
            else:
                await adapter._handle_lark_event(_lark_event(OPEN_ID, text, message_id=f"om_{len(incoming)}"))

        async def start(adapter: Any, bus: Any) -> None:
            if channel_type == "telegram":
                adapter._acknowledge = lambda update: None
                await telegram_start(adapter, bus)
            else:
                adapter._bus = bus
                adapter._running = True
                adapter._transport = transport
            adapters.append(adapter)
            await receive(adapter)

        async def message_during_check() -> None:
            if adapters:
                await receive(adapters[0])
            else:
                # Telegram queues these until start_polling deletes the old updates.
                text = f"backlog {len(incoming)}"
                incoming.append(text)
                pending.append(text)

        check = serve.self_check.Check("test", "Test", serve.self_check.WARN, "unavailable")
        loop = asyncio.get_running_loop()

        def sync_check(*args: Any) -> Any:
            asyncio.run_coroutine_threadsafe(message_during_check(), loop).result(timeout=2)
            return check

        async def async_check() -> Any:
            await message_during_check()
            return check

        load = managers.ManagerFleet.load

        async def load_with_backlog(fleet: Any) -> None:
            # An already polling adapter can receive another message while the fleet is still being built.
            for adapter in adapters:
                await receive(adapter)
            await load(fleet)

        register = serve.ChannelRegistry.register

        async def register_and_keep(registry: Any, adapter: Any) -> None:
            if registry not in registries:
                registries.append(registry)
            await register(registry, adapter)

        async def ready(supervisor: Any, agent_id: str) -> None:
            # The real start_all has replayed the journal; inspect before any agent consumes its queue.
            raise Started

        identity = SimpleNamespace(profile="", app_id="cli_x", bot_name="B", bot_open_id="ou_bot00000001")
        monkeypatch.setattr(lark_cli, "identity", AsyncMock(return_value=identity))
        monkeypatch.setattr(lark_cli, "check", AsyncMock(return_value=(None, identity)))
        monkeypatch.setattr(TelegramChannelAdapter if channel_type == "telegram" else LarkChannelAdapter, "start", start)
        monkeypatch.setattr(managers.ManagerFleet, "load", load_with_backlog)
        monkeypatch.setattr(serve.ChannelRegistry, "register", register_and_keep)
        monkeypatch.setattr(serve.AgentSupervisor, "_start_agent", ready)
        monkeypatch.setattr(managers_api, "_listener", None)
        monkeypatch.setattr(operator_notes, "_root", str(tmp_path))
        monkeypatch.setattr(db, "init_pool", AsyncMock(return_value=pool))
        monkeypatch.setattr(profile_registry, "register_found_runtimes", AsyncMock(return_value=[]))
        for module in (codex_login, grok_login):
            monkeypatch.setattr(module, "inspect_at_start", AsyncMock(return_value=None))
        monkeypatch.setattr(serve.self_check, "check_database", AsyncMock(return_value=serve.self_check.Check(
            "database", "Database", serve.self_check.OK, "")))
        for name in ("codex_login_check", "grok_login_check"):
            monkeypatch.setattr(serve.self_check, name, async_check)
        monkeypatch.setattr(serve.self_check, "check_claude", lambda _: [sync_check()])
        monkeypatch.setattr(serve.self_check, "check_git", sync_check)
        monkeypatch.setattr(serve.self_check, "check_lark_cli", lambda _: check)
        ops = ops_config.OpsConfig(runtime_root=tmp_path, path=tmp_path / "ops.toml", database_url=DSN, port=9876,
                                   operator_notes_root=str(tmp_path))
        try:
            with pytest.raises(Started):
                await asyncio.wait_for(serve._run_agent_system(ops=ops, runtime_root=tmp_path, work_dir=tmp_path,
                                                               skip_backup=True), 10)
            assert adapters and incoming
            assert len(incoming) == 5
            if channel_type == "telegram":
                assert pending == []
            queued = await adapters[0]._bus.drain_queue(manager_id)
            replies = [s["text"] for s in bot.sent] if channel_type == "telegram" else [
                fields["text"] for method, fields in transport.calls if method == "send" and "text" in fields]
            if enabled:
                assert [message.payload["text"] for message in queued] == incoming
                assert TURNED_OFF_REPLY not in replies
            else:
                assert queued == []
                assert replies == [TURNED_OFF_REPLY] * len(incoming)
                assert adapters[0]._bus.journal.undelivered() == []
        finally:
            for registry in registries:
                await registry.stop_all()

    run_with_channel_configs(test, monkeypatch)


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

    run_with_channel_configs(test, monkeypatch)
