"""The real PTB polling/application lifecycle against a stand-in Bot API."""

from __future__ import annotations

import asyncio
import json
import sqlite3
import time
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from looper.agents.bus import MessageBus
from looper.agents.channels.allowlist import AllowlistError, SenderAllowlist, normalized_config
from looper.agents.channels.telegram import TelegramChannelAdapter
from looper.agents.channels.telegram_inbox import TelegramInbox


class BotAPI:
    def __init__(self, inbox: Path) -> None:
        self.inbox = inbox
        self.updates: list[dict] = []
        self.offsets: list[int] = []
        self.drops: list[bool] = []
        self.changed = asyncio.Event()

    def send(self, update_id: int, text: str, *, age: int = 0, sent_at: int | None = None) -> None:
        self.updates.append({"update_id": update_id, "message": {
            "message_id": update_id, "date": int(time.time()) - age if sent_at is None else sent_at, "text": text,
            "chat": {"id": 7, "type": "private"},
            "from": {"id": 7, "is_bot": False, "first_name": "Synthetic"}}})
        self.changed.set()

    async def request(self, url: str, request_data=None, **kwargs):
        method = url.rsplit("/", 1)[-1]
        data = request_data.parameters if request_data is not None else {}
        if method == "getMe":
            result = {"id": 1, "is_bot": True, "first_name": "Synthetic", "username": "synthetic_bot"}
        elif method == "deleteWebhook":
            self.drops.append(data.get("drop_pending_updates", False))
            if self.drops[-1]:
                self.updates.clear()
            result = True
        elif method == "getUpdates":
            offset = int(data.get("offset", 0))
            self.offsets.append(offset)
            # PTB advances the offset after enqueueing, before awaiting handlers.
            # Every update being acknowledged must already have a durable row.
            if offset:
                with sqlite3.connect(self.inbox) as db:
                    for update in self.updates:
                        if update["update_id"] < offset:
                            assert db.execute("SELECT 1 FROM updates WHERE update_id = ?",
                                              (update["update_id"],)).fetchone()
            result = [update for update in self.updates if update["update_id"] >= offset]
            if not result and data.get("timeout", 0):
                self.changed.clear()
                try:
                    await asyncio.wait_for(self.changed.wait(), .05)
                except TimeoutError:
                    pass
                result = [update for update in self.updates if update["update_id"] >= offset]
        else:
            assert method in {"setMyCommands", "setMessageReaction"}, method
            result = True
        return 200, json.dumps({"ok": True, "result": result}).encode()


@pytest.mark.parametrize("backlog_seconds", [600, 30])
@pytest.mark.parametrize("gap_seconds", [0, 1200])
def test_restart_delivers_gap_updates_once_and_logs_stale_backlog(
    tmp_path, monkeypatch, caplog, backlog_seconds, gap_seconds,
):
    from looper.agents.channels import telegram as channel
    from telegram.request import HTTPXRequest

    now = int(time.time()) - gap_seconds

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime.fromtimestamp(now, tz=tz)

    monkeypatch.setattr(channel, "datetime", Clock)

    async def run():
        nonlocal now
        inbox = tmp_path / ".local" / "telegram" / "channel-telegram" / "0.sqlite3"
        api = BotAPI(inbox)

        async def request(transport, **kwargs):
            return await api.request(**kwargs)

        monkeypatch.setattr(HTTPXRequest, "do_request", request)
        bus = MessageBus(tmp_path)
        await bus.start()

        def adapter():
            return TelegramChannelAdapter(bot_token="0:synthetic", allowlist=SenderAllowlist(
                "channel-telegram", frozenset({"7"})), backlog_max_age_seconds=backlog_seconds)

        first, second, third = adapter(), adapter(), adapter()
        try:
            await first.start(bus)
            api.send(99, "stale on first start", sent_at=now - backlog_seconds - 1)
            api.send(100, "before restart", sent_at=now)
            queue = bus.get_queue("manager")
            before = await asyncio.wait_for(queue.get(), 2)
            await first.stop()
            now += gap_seconds
            bus.settle(before.message_id)
            api.send(101, "during gap", sent_at=now - max(0, gap_seconds - 10))
            api.send(102, "stale private content", sent_at=now - gap_seconds - backlog_seconds - 1)
            await second.start(bus)
            gap = await asyncio.wait_for(queue.get(), 2)
            assert gap.payload["text"] == "during gap"
            await asyncio.wait_for(second._app.update_queue.join(), 2)
            await second.stop()
            assert queue.empty()
            # Stand-in deliberately redelivers the acknowledged batch on restart.
            # Dedupe survives both Application and adapter recreation.
            await third.start(bus)
            await asyncio.wait_for(third._app.update_queue.join(), 2)
            await third.stop()
            assert queue.empty()
            assert api.drops == [False, False, False]
            assert 103 in api.offsets
            assert "stale Telegram update_id=102" in caplog.text
            assert "stale Telegram update_id=99" in caplog.text
            assert "stale private content" not in caplog.text
            with sqlite3.connect(inbox) as db:
                assert db.execute("SELECT update_id, payload, processed FROM updates ORDER BY update_id").fetchall() == [
                    (99, None, 1), (100, None, 1), (101, None, 1), (102, None, 1)]
        finally:
            for item in (first, second, third):
                await item.stop()
            await bus.shutdown()

    asyncio.run(asyncio.wait_for(run(), 10))


def test_crash_does_not_reuse_an_older_clean_stop_boundary(tmp_path, caplog):
    from telegram import Bot, Update

    async def run():
        path = tmp_path / "inbox.sqlite3"
        stopped_at = int(time.time()) - 2400
        first = TelegramInbox(path)
        first.polling_stopped(stopped_at)
        first.close()
        second = TelegramInbox(path)
        assert second.previous_polling_stopped_at == stopped_at
        second.polling_started()
        assert second.previous_polling_stopped_at == stopped_at
        # Simulate process loss: close without recording a clean polling stop.
        second.close()
        third = TelegramInbox(path)
        adapter = TelegramChannelAdapter(bot_token="0:synthetic", allowlist=SenderAllowlist(
            "channel-telegram", frozenset({"7"})))
        adapter._inbox = third
        try:
            assert third.previous_polling_stopped_at is None
            update = Update.de_json({"update_id": 23, "message": {
                "message_id": 23, "date": stopped_at + 10,
                "chat": {"id": 7, "type": "private"}, "text": "old pending content"}}, Bot("0:synthetic"))
            await third.put(update)
            callback = AsyncMock()
            await adapter._durable(callback)(await third.get(), None)
            third.task_done()
            callback.assert_not_awaited()
            assert third.processed(23)
            assert "stale Telegram update_id=23" in caplog.text
            assert "old pending content" not in caplog.text
        finally:
            third.close()

    asyncio.run(run())


def test_acknowledged_update_replays_when_handler_never_started(tmp_path):
    from telegram import Bot, Update

    async def run():
        path = tmp_path / "inbox.sqlite3"
        first = TelegramInbox(path)
        bot = Bot("0:synthetic")
        raw = {"update_id": 22, "message": {"message_id": 8, "date": int(time.time()),
               "chat": {"id": 7, "type": "private"}, "text": "queued"}}
        await first.put(Update.de_json(raw, bot))
        first.close()
        second = TelegramInbox(path)
        try:
            await second.replay(bot)
            update = second.get_nowait()
            assert update.update_id == 22 and update.message.text == "queued"
            second.finish(22)
            assert second.processed(22)
        finally:
            second.close()

    asyncio.run(run())


@pytest.mark.parametrize("value", [0, -1, True, "600", None])
def test_backlog_limit_refuses_invalid_channel_config(value):
    with pytest.raises(AllowlistError, match="backlog_max_age_seconds"):
        normalized_config("telegram", {"backlog_max_age_seconds": value})


def test_replacing_bot_on_same_channel_does_not_reuse_another_bots_dedupe_keys(tmp_path, monkeypatch):
    from telegram.request import HTTPXRequest

    async def run():
        directory = tmp_path / ".local/telegram/channel-telegram"
        api = BotAPI(directory / "0.sqlite3")

        async def request(transport, **kwargs):
            return await api.request(**kwargs)

        monkeypatch.setattr(HTTPXRequest, "do_request", request)
        bus = MessageBus(tmp_path)
        await bus.start()
        adapters = []
        try:
            for bot_id, text in ((0, "old bot"), (1, "replacement bot")):
                api.inbox = directory / f"{bot_id}.sqlite3"
                api.updates.clear()
                api.send(22, text)
                adapter = TelegramChannelAdapter(bot_token=f"{bot_id}:synthetic", allowlist=SenderAllowlist(
                    "channel-telegram", frozenset({"7"})))
                adapters.append(adapter)
                await adapter.start(bus)
                message = await asyncio.wait_for(bus.get_queue("manager").get(), 2)
                assert message.payload["text"] == text
                await adapter.stop()
        finally:
            for adapter in adapters:
                await adapter.stop()
            await bus.shutdown()

    asyncio.run(asyncio.wait_for(run(), 10))
