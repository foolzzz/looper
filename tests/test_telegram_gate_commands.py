"""Telegram registers /gates and /gate and lists them in the bot's menu; the handlers pass the text, as typed, to the
Manager (which answers in code, see ``test_channel_gate_commands``)."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from looper.agents.bus import MessageBus
from looper.agents.channels.allowlist import SenderAllowlist
from looper.agents.channels.telegram import _BOT_COMMANDS, TelegramChannelAdapter
from looper.agents.message import MessageEnvelope, MessageType


def _update(user_id: int, text: str) -> Any:
    return SimpleNamespace(
        effective_user=SimpleNamespace(id=user_id, username="someone"),
        effective_chat=SimpleNamespace(id=99),
        message=SimpleNamespace(text=text, reply_text=AsyncMock(), message_id=41, reply_to_message=None),
    )


def _run(tmp_path: Path, handler: str, update: Any, *ids: str) -> list[MessageEnvelope]:
    async def _main() -> list[MessageEnvelope]:
        bus = MessageBus(runtime_root=tmp_path)
        adapter = TelegramChannelAdapter(
            bot_token="0:not-a-token", allowlist=SenderAllowlist("channel-telegram", frozenset(ids)))
        adapter._bus = bus
        await getattr(adapter, handler)(update, None)
        queue = bus.get_queue("manager")
        messages = []
        while not queue.empty():
            messages.append(queue.get_nowait())
        await bus.shutdown()
        return messages

    return asyncio.run(_main())


@pytest.mark.parametrize(("handler", "text"), [
    ("_cmd_gates", "/gates"),
    ("_cmd_gates", "/gates@helper_bot"),
    ("_cmd_gate", "/gate 1 用 Redis"),
    ("_cmd_gate", "/gate@helper_bot 2 end"),
])
def test_the_commands_reach_the_manager_as_typed(tmp_path: Path, handler: str, text: str) -> None:
    (message,) = _run(tmp_path, handler, _update(7, text), "7")
    assert message.message_type == MessageType.USER_MESSAGE
    assert message.payload["text"] == text
    assert message.payload["channel_context"]["user_id"] == 7
    assert message.payload["channel_context"]["message_id"] == "41"


@pytest.mark.parametrize("handler", ["_cmd_gates", "_cmd_gate"])
def test_an_unlisted_sender_gets_nothing(tmp_path: Path, handler: str) -> None:
    update = _update(8, "/gate 1 end")
    assert _run(tmp_path, handler, update, "7") == []
    update.message.reply_text.assert_not_awaited()


def test_the_menu_lists_both_commands() -> None:
    names = [name for name, _ in _BOT_COMMANDS]
    assert "gates" in names and "gate" in names


def test_start_registers_the_handlers_and_publishes_the_menu(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from telegram.ext import CommandHandler

    async def _main() -> tuple[set[str], list[str]]:
        bus = MessageBus(runtime_root=tmp_path)
        adapter = TelegramChannelAdapter(
            bot_token="0:not-a-token", allowlist=SenderAllowlist("channel-telegram", frozenset({"7"})))
        handlers: list[Any] = []
        app = MagicMock()
        app.add_handler = handlers.append
        app.initialize = AsyncMock()
        app.start = AsyncMock()
        app.bot.set_my_commands = AsyncMock()
        app.updater.start_polling = AsyncMock()
        app.updater.stop = AsyncMock()
        app.stop = AsyncMock()
        app.shutdown = AsyncMock()
        builder = MagicMock()
        builder.token.return_value.build.return_value = app
        monkeypatch.setattr("telegram.ext.Application.builder", lambda: builder)
        await adapter.start(bus)
        commands = {c for h in handlers if isinstance(h, CommandHandler) for c in h.commands}
        menu = [c.command for c in app.bot.set_my_commands.await_args.args[0]]
        await adapter.stop()
        await bus.shutdown()
        return commands, menu

    commands, menu = asyncio.run(_main())
    assert {"gates", "gate"} <= commands
    assert {"gates", "gate"} <= set(menu)
