"""A schedule's message through Telegram and Lark: what each adapter sends and to whom, with stand-ins for the
Telegram bot and for ``lark-cli`` (nothing reaches a real account), and the registry's answer for a channel that
is not running."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from fake_claude import fake_claude
from looper.agents.channels.allowlist import SenderAllowlist
from looper.agents.channels.lark import LarkChannelAdapter
from looper.agents.channels.registry import ChannelRegistry
from looper.agents.channels.telegram import TelegramChannelAdapter


class _Bot:
    def __init__(self, fail: bool = False) -> None:
        self.sent: list[dict[str, Any]] = []
        self.fail = fail

    async def send_message(self, **kwargs: Any) -> Any:
        if self.fail:
            raise RuntimeError("Forbidden: bot was blocked by the user")
        self.sent.append(kwargs)
        return SimpleNamespace(message_id=len(self.sent))


def test_telegram_sends_plain_text_to_the_users_private_chat() -> None:
    adapter = TelegramChannelAdapter(bot_token="0:not-a-token",
                                     allowlist=SenderAllowlist("channel-telegram", frozenset({"1000001"})))
    bot = _Bot()
    adapter._app = SimpleNamespace(bot=bot)
    receipt = asyncio.run(adapter.notify("1000001", "定时任务「triage」：失败"))
    assert receipt.success and bot.sent == [{"chat_id": 1000001, "text": "定时任务「triage」：失败"}]

    adapter._app = SimpleNamespace(bot=_Bot(fail=True))
    receipt = asyncio.run(adapter.notify("1000001", "x"))
    assert not receipt.success and "blocked" in (receipt.error or "")


def test_lark_sends_as_the_bot_to_the_users_open_id(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    log = tmp_path / "argv.json"
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake_claude(bin_dir / "lark-cli",
                f"import json, sys\njson.dump(sys.argv[1:], open({str(log)!r}, 'w'))\n"
                "sys.exit(0 if '--user-id' in sys.argv else 2)\n")
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    adapter = LarkChannelAdapter(app_id="cli_test", runtime_root=tmp_path,
                                 allowlist=SenderAllowlist("channel-lark", frozenset({"ou_abcdefgh12"})))
    receipt = asyncio.run(adapter.notify("ou_abcdefgh12", "告警：定时任务「triage」已连续失败 3 次。"))
    assert receipt.success
    assert json.loads(log.read_text()) == ["im", "+messages-send", "--as", "bot", "--user-id", "ou_abcdefgh12",
                                           "--text", "告警：定时任务「triage」已连续失败 3 次。"]


def test_a_channel_that_is_not_running_says_so() -> None:
    registry = ChannelRegistry(bus=SimpleNamespace())
    sent, error = asyncio.run(registry.notify("telegram", "1000001", "x"))
    assert not sent and error == "the telegram channel is not running: enable it in Settings → Channels"
