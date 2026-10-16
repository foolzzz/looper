"""The Telegram bot token never reaches the log.

The Bot API puts the token in every request URL's path, and httpx logs each request URL at INFO. These tests run
the real adapter (python-telegram-bot and its httpx client) against a fake Telegram, an httpx ``MockTransport``,
under the logging setup of ``looper serve``, and read what it writes to stderr.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest

from looper.agents import mcp_server
from looper.agents.bus import MessageBus
from looper.agents.channels.allowlist import SenderAllowlist
from looper.agents.channels.telegram import TelegramChannelAdapter
from looper.agents.log_redaction import RedactingFormatter
from looper.agents.message import TASK_ROUTER_ADDRESS, MessageEnvelope, MessageType, channel_address
from looper.agents.serve import _configure_logging

# The shape of a BotFather token; not a real one.
TOKEN = "123456789:AAFakeTokenForTheRedactionTest_0123456"
SECRET = TOKEN.split(":", 1)[1]


@pytest.fixture
def serve_logging() -> Iterator[None]:
    """Takes the stderr handler and the levels a logging setup leaves off again after the test."""
    levels = {name: logging.getLogger(name).level for name in ("", "httpx", "httpcore")}
    yield
    root = logging.getLogger()
    for handler in [h for h in root.handlers if isinstance(h.formatter, RedactingFormatter)]:
        root.removeHandler(handler)
    for name, level in levels.items():
        logging.getLogger(name).setLevel(level)


@pytest.fixture
def telegram_calls(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """python-telegram-bot's httpx clients talk to a fake Bot API; the list collects the methods called.

    setMyCommands and sendMessage fail with an error that names the request URL, so the token is in the
    exception that gets logged or returned.
    """
    calls: list[str] = []

    async def bot_api(request: httpx.Request) -> httpx.Response:
        method = request.url.path.rsplit("/", 1)[-1]
        calls.append(method)
        if method in ("setMyCommands", "sendMessage"):
            raise httpx.ConnectError(f"cannot reach {request.url}", request=request)
        if method == "getUpdates":
            await asyncio.sleep(0.02)  # a long poll that ends without updates
        result = {
            "getMe": {"id": 123456789, "is_bot": True, "first_name": "Looper", "username": "looper_test_bot"},
            "getUpdates": [],
        }.get(method, True)
        return httpx.Response(200, json={"ok": True, "result": result})

    class FakeTelegramClient(httpx.AsyncClient):
        def __init__(self, **kwargs: Any) -> None:
            super().__init__(**{**kwargs, "transport": httpx.MockTransport(bot_api), "trust_env": False})

    monkeypatch.setattr(httpx, "AsyncClient", FakeTelegramClient)
    return calls


def _run_adapter(
    tmp_path: Path, calls: list[str], then: Callable[[TelegramChannelAdapter], Awaitable[Any]] | None = None,
) -> Any:
    """Start the adapter, wait for its first poll, run ``then`` on it, stop it; ``then``'s result."""

    async def main() -> Any:
        bus = MessageBus(runtime_root=tmp_path)
        adapter = TelegramChannelAdapter(
            bot_token=TOKEN, allowlist=SenderAllowlist("channel-telegram", frozenset({"7"})),
        )
        await adapter.start(bus)
        async with asyncio.timeout(10):
            while "getUpdates" not in calls:
                await asyncio.sleep(0.01)
        result = await then(adapter) if then else None
        await adapter.stop()
        await bus.shutdown()
        return result

    return asyncio.run(main())


@pytest.mark.parametrize("verbose", [False, True], ids=["default", "verbose"])
def test_the_token_stays_out_of_the_serve_log(
    verbose: bool, tmp_path: Path, serve_logging: None, telegram_calls: list[str],
    capsys: pytest.CaptureFixture[str],
) -> None:
    _configure_logging(verbose)
    _run_adapter(tmp_path, telegram_calls)

    log = capsys.readouterr().err
    assert "telegram channel adapter started" in log
    assert SECRET not in log
    # The failed setMyCommands is logged with its traceback, whose exceptions name the URL.
    assert "Traceback" in log and "https://api.telegram.org/bot***/setMyCommands" in log
    requests = [line for line in log.splitlines() if "HTTP Request:" in line]
    if verbose:
        assert any("https://api.telegram.org/bot***/getUpdates" in line for line in requests)
    else:
        assert requests == []  # httpx logs every request at INFO; without --verbose only from WARNING


def test_the_configured_token_is_masked_outside_a_url(
    tmp_path: Path, serve_logging: None, telegram_calls: list[str], capsys: pytest.CaptureFixture[str],
) -> None:
    _configure_logging(False)

    async def log_the_bot(adapter: TelegramChannelAdapter) -> None:
        logging.getLogger("looper.test").warning("bot: %r", adapter._app.bot)  # python-telegram-bot's Bot[token=…]

    _run_adapter(tmp_path, telegram_calls, log_the_bot)

    log = capsys.readouterr().err
    assert "Bot[token=***]" in log and SECRET not in log


def _chat_notice(adapter: TelegramChannelAdapter) -> Awaitable[Any]:
    return adapter.deliver(MessageEnvelope.create(
        source=TASK_ROUTER_ADDRESS, target=channel_address("telegram", adapter.channel_id),
        message_type=MessageType.CHAT_NOTICE, payload={"chat": "7", "text": "done"},
    ))


@pytest.mark.parametrize("send", [lambda adapter: adapter.notify("7", "hello"), _chat_notice], ids=["notify", "answer"])
def test_a_failed_send_does_not_return_the_token(
    send: Callable[[TelegramChannelAdapter], Awaitable[Any]], tmp_path: Path, telegram_calls: list[str],
) -> None:
    receipt = _run_adapter(tmp_path, telegram_calls, send)

    assert not receipt.success
    assert "https://api.telegram.org/bot***/sendMessage" in receipt.error and SECRET not in receipt.error


@pytest.mark.parametrize("configure", [lambda: _configure_logging(False), mcp_server.main], ids=["serve", "mcp-server"])
def test_every_entry_point_masks_a_bot_url(
    configure: Callable[[], Any], serve_logging: None, capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("LOOPER_AGENT_URL", raising=False)  # mcp-server returns right after setting up its log
    other = "987654321:AAAnotherFakeTokenNobodyConfigured_987"  # not configured anywhere: only its URL shape tells

    configure()
    logging.getLogger("looper.test").warning("POST https://api.telegram.org/bot%s/getMe failed", other)

    err = capsys.readouterr().err
    assert "POST https://api.telegram.org/bot***/getMe failed" in err and other.split(":")[1] not in err
