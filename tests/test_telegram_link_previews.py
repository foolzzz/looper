"""Agent-authored Telegram text must not make Telegram fetch the URLs it contains (link previews)."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from looper.agents.bus import MessageBus
from looper.agents.channels.allowlist import SenderAllowlist
from looper.agents.channels.telegram import TelegramChannelAdapter


@pytest.mark.parametrize("method", ["send_message", "edit_message_text"])
def test_bot_requests_disable_link_previews(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, method: str) -> None:
    """Every text write goes through the adapter's bot; a request carries ``link_preview_options.is_disabled``."""
    from telegram.error import TimedOut
    from telegram.ext import ApplicationBuilder
    from telegram.request import HTTPXRequest

    async def run() -> None:
        bus = MessageBus(runtime_root=tmp_path)
        adapter = TelegramChannelAdapter(
            bot_token="0:synthetic", allowlist=SenderAllowlist("channel-telegram", frozenset({"7"})))
        apps = []
        sent: list[tuple[str, dict]] = []
        build = ApplicationBuilder.build

        def build_app(builder):
            app = build(builder)
            apps.append(app)
            return app

        async def do_request(request, url, **kwargs):
            name = url.rsplit("/", 1)[-1]
            if name == "getMe":
                raise TimedOut("stop after the app is built")
            data = kwargs["request_data"].parameters
            sent.append((name, data))
            return 200, json.dumps({"ok": True, "result": {
                "message_id": 5, "date": 0, "chat": {"id": 1, "type": "private"}, "text": "x"}}).encode()

        monkeypatch.setattr(ApplicationBuilder, "build", build_app)
        monkeypatch.setattr(HTTPXRequest, "do_request", do_request)
        try:
            with pytest.raises(TimedOut):
                await adapter.start(bus)
            bot = apps[0].bot
            text = "see https://example.invalid/leak?d=secret"
            if method == "send_message":
                await bot.send_message(chat_id=1, text=text)
            else:
                await bot.edit_message_text(chat_id=1, message_id=5, text=text)
            assert len(sent) == 1
            options = sent[0][1].get("link_preview_options")
            assert options and options["is_disabled"] is True
            await adapter.stop()
        finally:
            if adapter._outbound_task:
                adapter._outbound_task.cancel()
                await asyncio.gather(adapter._outbound_task, return_exceptions=True)
            await bus.shutdown()

    asyncio.run(run())
