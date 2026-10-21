"""/profile from Telegram and Lark reaches the Manager as the text ``/profile [choice]``, behind the sender allowlist."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from looper.agents.bus import MessageBus
from looper.agents.message import MessageType
from looper.agents.channels.telegram import _BOT_COMMANDS
from tests.test_agent_channels import (
    BOT_MENTION,
    OPEN_ID,
    OTHER_OPEN_ID,
    TestLarkAllowlist,
    _allowlist,
    _lark_event,
    _manager_messages,
    _tg_update,
)


def _telegram(tmp_path: Path, user_id: int, text: str, args: list[str]) -> list[Any]:
    from looper.agents.channels.telegram import TelegramChannelAdapter

    async def _main() -> list[Any]:
        bus = MessageBus(runtime_root=tmp_path)
        adapter = TelegramChannelAdapter(bot_token="0:not-a-token", allowlist=_allowlist("channel-telegram", "7"))
        adapter._bus = bus  # what start() sets, without polling Telegram
        await adapter._cmd_profile(_tg_update(user_id, text), SimpleNamespace(args=args))
        messages = _manager_messages(bus)
        await adapter._reactions.stop()
        await bus.shutdown()
        return messages

    return asyncio.run(_main())


class TestTelegramProfile:
    @pytest.mark.parametrize(("text", "args", "sent"), [
        ("/profile", [], "/profile"),
        ("/profile 2", ["2"], "/profile 2"),
        ("/profile@MyBot 2", ["2"], "/profile 2"),
        ("/profile my  profile", ["my", "profile"], "/profile my profile"),
    ])
    def test_a_listed_sender_publishes_the_command(self, tmp_path: Path, text: str, args: list[str],
                                                   sent: str) -> None:
        (message,) = _telegram(tmp_path, 7, text, args)
        assert message.message_type == MessageType.USER_MESSAGE
        assert message.payload["text"] == sent

    def test_an_unlisted_sender_publishes_nothing(self, tmp_path: Path) -> None:
        assert _telegram(tmp_path, 8, "/profile", []) == []

    def test_the_menu_lists_profile(self) -> None:
        assert "profile" in [name for name, _ in _BOT_COMMANDS]


class TestLarkProfile:
    @pytest.mark.parametrize(("fields", "text", "sent"), [
        ({}, "/profile", "/profile"),
        ({"chat_type": "group", "chat_id": "oc_team", "mentions": [BOT_MENTION]}, "@HelperBot /profile 2",
         "/profile 2"),
        ({"chat_type": "group", "chat_id": "oc_team", "mentions": [BOT_MENTION]}, "@_user_1 /profile 2",
         "/profile 2"),
        ({"chat_type": "group", "chat_id": "oc_team", "mentions": [BOT_MENTION]}, "/profile  2 @HelperBot",
         "/profile 2"),
    ])
    def test_the_command_reaches_the_manager_as_typed(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                                      fields: dict[str, Any], text: str, sent: str) -> None:
        got = TestLarkAllowlist()._run(tmp_path, monkeypatch, _lark_event(OPEN_ID, text, **fields), OPEN_ID)
        (message,) = got.messages
        assert message.payload["text"] == sent

    def test_an_unlisted_sender_publishes_nothing(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        got = TestLarkAllowlist()._run(tmp_path, monkeypatch, _lark_event(OTHER_OPEN_ID, "/profile"), OPEN_ID)
        assert got.messages == []
