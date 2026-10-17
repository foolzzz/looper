"""A user gate's messages through Telegram and Lark (``ChannelAdapter.post``): what each adapter sends, to whom, and
the ids of the messages it sent, with stand-ins for the Telegram bot and for ``lark-cli`` (nothing reaches a real
chat or account); the registry's answer for a channel that is not running; and the default of a channel that cannot
post. The reply references of inbound messages are tested with the adapters' handlers (``test_agent_channels``)."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from lark_cli_stand_in import OPEN_ID
from looper.agents.bus import MessageBus
from looper.agents.channels import lark_cli
from looper.agents.channels.allowlist import SenderAllowlist
from looper.agents.channels.base import ChannelAdapter, DeliveryReceipt, PostReceipt
from looper.agents.channels.lark import LarkChannelAdapter
from looper.agents.channels.registry import ChannelRegistry
from looper.agents.channels.telegram import TelegramChannelAdapter
from looper.agents.channels.web import WebChannelAdapter
from looper.agents.message import MessageEnvelope
from looper.agents.task_notice import NotifyTarget, TargetKind

LARK_USER = NotifyTarget(TargetKind.USER, OPEN_ID)
LARK_GROUP = NotifyTarget(TargetKind.CHAT, "oc_group", mention=OPEN_ID)
TELEGRAM_GROUP = NotifyTarget(TargetKind.CHAT, "-1001234567")
CARD = "⏸ 需要你决定 · calc\n1. 用哪个库？（选项：A / B）\n直接回复这条消息作答。"


# --- Lark ---


def _lark(tmp_path: Path) -> LarkChannelAdapter:
    return LarkChannelAdapter(profile="other", runtime_root=tmp_path,
                              allowlist=SenderAllowlist("channel-lark", frozenset({OPEN_ID})))


def _calls(log: Path) -> list[list[str]]:
    return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []


@pytest.mark.parametrize(("target", "reply_to", "command", "sent"), [
    # A direct message to the user; a group @-mentions the requester, as a task's end notice does.
    (LARK_USER, None, ["+messages-send", "--as", "bot", "--user-id", OPEN_ID, "--markdown", CARD], "om_0001"),
    (LARK_GROUP, None, ["+messages-send", "--as", "bot", "--chat-id", "oc_group", "--markdown",
                        f'<at user_id="{OPEN_ID}"></at> {CARD}'], "om_0001"),
    (NotifyTarget(TargetKind.CHAT, "oc_group"), None,
     ["+messages-send", "--as", "bot", "--chat-id", "oc_group", "--markdown", CARD], "om_0001"),
    # A reply quotes the message it answers and @-mentions nobody, in a group too.
    (LARK_GROUP, "om_card", ["+messages-reply", "--as", "bot", "--message-id", "om_card", "--markdown", CARD],
     "om_r0001"),
    (LARK_USER, "om_card", ["+messages-reply", "--as", "bot", "--message-id", "om_card", "--markdown", CARD],
     "om_r0001"),
    # An empty reference is none.
    (LARK_USER, "", ["+messages-send", "--as", "bot", "--user-id", OPEN_ID, "--markdown", CARD], "om_0001"),
])
def test_lark_posts_markdown_as_the_bot_and_returns_the_message_it_sent(
    tmp_path: Path, lark_cli_stand_in: Path, target: NotifyTarget, reply_to: str | None, command: list[str],
    sent: str,
) -> None:
    receipt = asyncio.run(_lark(tmp_path).post(target, CARD, reply_to=reply_to))
    assert receipt == PostReceipt(True, external_ids=(sent,))
    assert _calls(lark_cli_stand_in) == [["--profile", "other", "im", *command]]


@pytest.mark.parametrize(("target", "reply_to", "error"), [
    (NotifyTarget(TargetKind.CHAT, "oc_gone"), None, "lark-cli exited 2: Bot/User can NOT be out of the chat"),
    (NotifyTarget(TargetKind.USER, "ou_gone"), None, "lark-cli exited 2: Bot/User can NOT be out of the chat"),
    (LARK_GROUP, "om_gone", "lark-cli exited 2: the message was recalled"),
])
def test_a_lark_post_that_lark_cli_refuses_says_why(
    tmp_path: Path, lark_cli_stand_in: Path, caplog: pytest.LogCaptureFixture, target: NotifyTarget,
    reply_to: str | None, error: str,
) -> None:
    with caplog.at_level("WARNING"):
        receipt = asyncio.run(_lark(tmp_path).post(target, CARD, reply_to=reply_to))
    assert receipt == PostReceipt(False, error=error)
    assert f"lark: a post to {target.id} failed: {error}" in caplog.text


@pytest.mark.parametrize(("printed", "error"), [
    # Sent, by its exit, but without the id a reply to it would be routed by.
    (b'{"ok": true, "identity": "bot", "data": {"chat_id": "oc_group"}}', "lark-cli did not say which message it sent"),
    (b'{"ok": true, "identity": "bot"}', "lark-cli did not say which message it sent"),
    (b'{"ok": false, "error": {"message": "rate limited"}}', "rate limited"),
    (b"sent!", "lark-cli printed something that is not JSON: b'sent!'"),
])
def test_a_lark_post_without_a_message_id_failed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, printed: bytes,
                                                 error: str) -> None:
    async def run(argv: list[str], **_: Any) -> bytes:
        return printed

    monkeypatch.setattr(lark_cli, "run", run)
    assert asyncio.run(_lark(tmp_path).post(LARK_USER, CARD)) == PostReceipt(False, error=error)


# --- Telegram ---


class _Bot:
    """A stand-in Telegram bot: it records what it is asked to send, and fails the send number ``fail_at``."""

    def __init__(self, fail_at: int | None = None) -> None:
        self.sent: list[dict[str, Any]] = []
        self.fail_at = fail_at

    async def send_message(self, **kwargs: Any) -> Any:
        if len(self.sent) == self.fail_at:
            raise RuntimeError("httpx.ConnectError: https://api.telegram.org/bot123456:AAE-secret_token/sendMessage")
        self.sent.append(kwargs)
        return SimpleNamespace(message_id=100 + len(self.sent))


def _telegram(bot: _Bot | None) -> TelegramChannelAdapter:
    adapter = TelegramChannelAdapter(bot_token="0:not-a-token",
                                     allowlist=SenderAllowlist("channel-telegram", frozenset({"7"})))
    adapter._app = None if bot is None else SimpleNamespace(bot=bot)  # what start() sets, without polling Telegram
    return adapter


@pytest.mark.parametrize(("reply_to", "extra"), [(None, {}), ("", {}), ("55", {"reply_to_message_id": 55})])
def test_telegram_posts_plain_text_to_the_chat(reply_to: str | None, extra: dict[str, int]) -> None:
    bot = _Bot()
    receipt = asyncio.run(_telegram(bot).post(TELEGRAM_GROUP, "*not* Markdown", reply_to=reply_to))
    assert receipt == PostReceipt(True, external_ids=("101",))
    # No parse_mode: the text is sent as it is.
    assert bot.sent == [{"chat_id": -1001234567, "text": "*not* Markdown", **extra}]


def test_a_long_telegram_post_is_split_and_only_its_first_message_is_the_reply() -> None:
    bot = _Bot()
    receipt = asyncio.run(_telegram(bot).post(TELEGRAM_GROUP, "x" * 9_000, reply_to="55"))
    assert receipt == PostReceipt(True, external_ids=("101", "102", "103"))
    assert [len(sent["text"]) for sent in bot.sent] == [4_000, 4_000, 1_000]
    assert [sent.get("reply_to_message_id") for sent in bot.sent] == [55, None, None]
    assert all(set(sent) <= {"chat_id", "text", "reply_to_message_id"} for sent in bot.sent)


def test_a_telegram_post_that_fails_part_way_keeps_the_ids_sent_and_hides_the_token(
    caplog: pytest.LogCaptureFixture,
) -> None:
    bot = _Bot(fail_at=1)
    with caplog.at_level("WARNING"):
        receipt = asyncio.run(_telegram(bot).post(TELEGRAM_GROUP, "x" * 9_000))
    # The first message went out: a reply to it still routes back.
    assert receipt == PostReceipt(False, external_ids=("101",),
                                  error="httpx.ConnectError: https://api.telegram.org/bot***/sendMessage")
    assert "secret_token" not in caplog.text and "failed (1 of its messages sent)" in caplog.text


@pytest.mark.parametrize(("bot", "target", "reply_to", "error"), [
    (None, TELEGRAM_GROUP, None, "the Telegram bot is not started"),
    (_Bot(), NotifyTarget(TargetKind.CHAT, "oc_group"), None, "not a Telegram chat id: 'oc_group'"),
    (_Bot(), TELEGRAM_GROUP, "om_card", "not a Telegram message id: 'om_card'"),
    (_Bot(fail_at=0), TELEGRAM_GROUP, None, "httpx.ConnectError: https://api.telegram.org/bot***/sendMessage"),
])
def test_a_telegram_post_that_cannot_be_sent_says_why(bot: _Bot | None, target: NotifyTarget, reply_to: str | None,
                                                      error: str) -> None:
    receipt = asyncio.run(_telegram(bot).post(target, "选 A", reply_to=reply_to))
    assert receipt == PostReceipt(False, error=error)
    assert bot is None or bot.sent == []


# --- The registry, and a channel that cannot post ---


class _Channel(ChannelAdapter):
    """A running channel that records what it is asked to post."""

    def __init__(self) -> None:
        self.posts: list[tuple[NotifyTarget, str, str | None]] = []

    @property
    def channel_type(self) -> str:
        return "lark"

    @property
    def channel_id(self) -> str:
        return "channel-lark"

    async def start(self, bus: MessageBus) -> None:
        pass

    async def stop(self) -> None:
        pass

    async def deliver(self, envelope: MessageEnvelope) -> DeliveryReceipt:
        return DeliveryReceipt(channel="lark", success=True)

    async def post(self, target: NotifyTarget, text: str, *, reply_to: str | None = None) -> PostReceipt:
        self.posts.append((target, text, reply_to))
        return PostReceipt(True, external_ids=("om_1",))


def test_the_registry_posts_through_the_running_channel_or_says_it_is_not_running() -> None:
    async def main() -> tuple[PostReceipt, PostReceipt, list[Any]]:
        registry = ChannelRegistry(bus=SimpleNamespace())
        channel = _Channel()
        await registry.register(channel)
        posted = await registry.post("lark", LARK_GROUP, CARD, reply_to="om_card")
        missing = await registry.post("telegram", TELEGRAM_GROUP, CARD)
        return posted, missing, channel.posts

    posted, missing, posts = asyncio.run(main())
    assert posted == PostReceipt(True, external_ids=("om_1",))
    assert posts == [(LARK_GROUP, CARD, "om_card")]
    assert missing == PostReceipt(False, error="the telegram channel is not running: enable it in Settings → Channels")


def test_a_channel_that_cannot_post_refuses() -> None:
    receipt = asyncio.run(WebChannelAdapter().post(LARK_USER, CARD, reply_to="om_card"))
    assert receipt == PostReceipt(False, error="the web channel cannot post messages")
