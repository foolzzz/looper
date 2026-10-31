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
            limit = int(data.get("limit", 100))
            result = [update for update in self.updates if update["update_id"] >= offset][:limit]
            if not result and data.get("timeout", 0):
                self.changed.clear()
                try:
                    await asyncio.wait_for(self.changed.wait(), .05)
                except TimeoutError:
                    pass
                result = [update for update in self.updates if update["update_id"] >= offset][:limit]
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
        second.polling_received([])
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


@pytest.mark.parametrize("received_before_failure", [0, 100])
def test_failed_recovery_then_restart_keeps_original_offline_boundary(tmp_path, monkeypatch, received_before_failure):
    from looper.agents.channels import telegram as channel
    from telegram.error import NetworkError
    from telegram.request import HTTPXRequest

    now = int(time.time()) - 2400

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime.fromtimestamp(now, tz=tz)

    monkeypatch.setattr(channel, "datetime", Clock)

    async def run():
        nonlocal now
        path = tmp_path / ".local/telegram/channel-telegram/0.sqlite3"
        api = BotAPI(path)
        failed = asyncio.Event()
        network_down = False

        async def request(transport, url, **kwargs):
            data = kwargs.get("request_data")
            offset = data.parameters.get("offset", 0) if data else 0
            if (network_down and url.endswith("/getUpdates")
                    and (not received_before_failure or offset >= 201)):
                failed.set()
                raise NetworkError("synthetic network outage")
            return await api.request(url, **kwargs)

        monkeypatch.setattr(HTTPXRequest, "do_request", request)
        bus = MessageBus(tmp_path)
        await bus.start()
        adapters = [TelegramChannelAdapter(bot_token="0:synthetic", allowlist=SenderAllowlist(
            "channel-telegram", frozenset({"7"}))) for _ in range(3)]
        try:
            await adapters[0].start(bus)
            await adapters[0].stop()
            stopped_at = now
            texts = [f"sent during upgrade {i}" for i in range(received_before_failure + 1)]
            for i, text in enumerate(texts, 101):
                api.send(i, text, sent_at=stopped_at + 10)
            now += 1200
            network_down = True
            await adapters[1].start(bus)
            await asyncio.wait_for(failed.wait(), 2)
            assert adapters[1].healthcheck()["healthy"]
            await adapters[1].stop()
            with sqlite3.connect(path) as db:
                assert db.execute("SELECT stopped_at FROM polling").fetchone() == (stopped_at,)
            network_down = False
            now += 1200
            await adapters[2].start(bus)
            queue = bus.get_queue("manager")
            messages = [await asyncio.wait_for(queue.get(), 2) for _ in texts]
            assert [message.payload["text"] for message in messages] == texts
            await asyncio.wait_for(adapters[2]._app.update_queue.join(), 2)
            assert bus.get_queue("manager").empty()
        finally:
            network_down = False
            for adapter in adapters:
                await adapter.stop()
            await bus.shutdown()

    asyncio.run(asyncio.wait_for(run(), 10))


@pytest.mark.parametrize("relative_date, delivered", [(-1, True), (0, False), (1, False)])
def test_downtime_exemption_ends_at_resume(tmp_path, monkeypatch, relative_date, delivered):
    from looper.agents.channels import telegram as channel
    from telegram.request import HTTPXRequest

    now = int(time.time()) - 2400

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime.fromtimestamp(now, tz=tz)

    monkeypatch.setattr(channel, "datetime", Clock)

    async def run():
        nonlocal now
        path = tmp_path / ".local/telegram/channel-telegram/0.sqlite3"
        api = BotAPI(path)
        acknowledged = asyncio.Event()

        async def request(transport, **kwargs):
            result = await api.request(**kwargs)
            data = kwargs.get("request_data")
            if kwargs["url"].endswith("/getUpdates") and data.parameters.get("offset", 0) == 102:
                acknowledged.set()
            return result

        monkeypatch.setattr(HTTPXRequest, "do_request", request)
        bus = MessageBus(tmp_path)
        await bus.start()
        adapters = [TelegramChannelAdapter(bot_token="0:synthetic", allowlist=SenderAllowlist(
            "channel-telegram", frozenset({"7"}))) for _ in range(2)]
        try:
            await adapters[0].start(bus)
            await adapters[0].stop()
            now += 1200
            resumed_at = now
            await adapters[1].start(bus)
            # The updater stays running across a later network gap or system sleep.
            now += 1200
            api.send(101, "delayed update", sent_at=resumed_at + relative_date)
            await asyncio.wait_for(acknowledged.wait(), 2)
            await asyncio.wait_for(adapters[1]._app.update_queue.join(), 2)
            queue = bus.get_queue("manager")
            assert (not queue.empty()) is delivered
            if delivered:
                assert queue.get_nowait().payload["text"] == "delayed update"
            assert adapters[1]._inbox.processed(101)
        finally:
            for adapter in adapters:
                await adapter.stop()
            await bus.shutdown()

    asyncio.run(asyncio.wait_for(run(), 10))


def test_shutdown_saves_offline_boundary_before_agent_drain(tmp_path, monkeypatch):
    from looper.agents.channels.registry import ChannelRegistry
    from looper.agents.serve import shut_down_within, stop_signal_handler
    from telegram.request import HTTPXRequest

    async def run():
        path = tmp_path / ".local/telegram/channel-telegram/0.sqlite3"
        api = BotAPI(path)

        async def request(transport, **kwargs):
            return await api.request(**kwargs)

        monkeypatch.setattr(HTTPXRequest, "do_request", request)
        bus = MessageBus(tmp_path)
        adapter = TelegramChannelAdapter(bot_token="0:synthetic", allowlist=SenderAllowlist(
            "channel-telegram", frozenset({"7"})))
        registry = ChannelRegistry(bus)
        registry._channels[adapter.channel_id] = adapter
        stop, draining = asyncio.Event(), asyncio.Event()
        exits = []

        async def shutdown():
            draining.set()
            await stop.wait()  # A Manager drain that outlasts the hard shutdown deadline.
            await registry.stop_all()

        try:
            await adapter.start(bus)
            handler = stop_signal_handler(asyncio.Event(), prepare_stop=registry.prepare_stop_all)
            before = int(time.time())
            handler()
            with sqlite3.connect(path) as db:
                boundary = db.execute("SELECT stopped_at FROM polling").fetchone()
            assert boundary is not None and before <= boundary[0] <= int(time.time())
            task = asyncio.create_task(shutdown())
            try:
                await shut_down_within(task, deadline=.05, exit_now=exits.append)
                assert draining.is_set() and exits == [1]
                assert adapter._app.updater.running
                with sqlite3.connect(path) as db:
                    assert db.execute("SELECT stopped_at FROM polling").fetchone() == boundary
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        finally:
            await registry.stop_all()
            await bus.shutdown()

    asyncio.run(asyncio.wait_for(run(), 10))


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


@pytest.mark.parametrize("failure_stage,failures,stopping,attempts", [
    ("before_publish", 2, False, 3),
    ("publish", 1, False, 1),
    ("after_publish", 1, False, 1),
    ("before_publish", 3, False, 3),
    ("before_publish", 1, True, 1),
])
def test_handler_failure_retry_and_restart(tmp_path, monkeypatch, caplog,
                                          failure_stage, failures, stopping, attempts):
    from telegram.request import HTTPXRequest

    async def run():
        path = tmp_path / ".local/telegram/channel-telegram/0.sqlite3"
        api = BotAPI(path)

        async def request(transport, **kwargs):
            return await api.request(**kwargs)

        monkeypatch.setattr(HTTPXRequest, "do_request", request)
        bus = MessageBus(tmp_path)
        await bus.start()
        adapter = TelegramChannelAdapter(bot_token="0:synthetic", allowlist=SenderAllowlist(
            "channel-telegram", frozenset({"7"})))
        original = adapter._on_message
        calls = 0
        failed = asyncio.Event()
        handled = asyncio.Event()

        async def handler(update, context):
            nonlocal calls
            calls += 1
            if failure_stage in {"publish", "after_publish"}:
                await original(update, context)
            if calls <= failures:
                if stopping:
                    adapter._running = False
                raise RuntimeError("synthetic handler failure")
            await original(update, context)
            handled.set()

        async def on_error(update, context):
            failed.set()

        monkeypatch.setattr(adapter, "_on_message", handler)
        if failure_stage == "publish":
            monkeypatch.setattr(bus, "publish", AsyncMock(side_effect=RuntimeError("synthetic handler failure")))
        try:
            await adapter.start(bus)
            adapter._app.add_error_handler(on_error)
            api.send(201, "retry this update")
            await asyncio.wait_for((handled if failures == 2 else failed).wait(), 5)
            await asyncio.wait_for(adapter._app.update_queue.join(), 2)
            assert calls == attempts
            success = failures == 2
            assert adapter._inbox.processed(201) is success
            assert not adapter._reached_publish
            queue = bus.get_queue("manager")
            delivered = success or failure_stage == "after_publish"
            if delivered:
                assert queue.get_nowait().payload["text"] == "retry this update"
            assert queue.empty()
            with sqlite3.connect(path) as db:
                payload, processed = db.execute("SELECT payload, processed FROM updates WHERE update_id = 201").fetchone()
                assert bool(processed) is success
                assert (payload is None) is success
            if not success:
                assert "Telegram update_id=201" in caplog.text
                assert "synthetic handler failure" in caplog.text
            await adapter.stop()
            if failures == 3:
                # Acknowledged by polling, but still pending: only the durable inbox can replay it.
                api.updates.clear()
                adapter = TelegramChannelAdapter(bot_token="0:synthetic", allowlist=SenderAllowlist(
                    "channel-telegram", frozenset({"7"})))
                await adapter.start(bus)
                replayed = await asyncio.wait_for(queue.get(), 2)
                assert replayed.payload["text"] == "retry this update"
                await asyncio.wait_for(adapter._app.update_queue.join(), 2)
                assert adapter._inbox.processed(201)
                assert not adapter._reached_publish and queue.empty()
        finally:
            await adapter.stop()
            await bus.shutdown()

    asyncio.run(asyncio.wait_for(run(), 10))


def test_handler_does_not_retry_when_stopped_during_backoff(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from telegram import Bot, Update

    from looper.agents.channels import telegram as channel

    async def run():
        inbox = TelegramInbox(tmp_path / "inbox.sqlite3")
        adapter = TelegramChannelAdapter(allowlist=SenderAllowlist("channel-telegram", frozenset({"7"})))
        adapter._inbox, adapter._running = inbox, True
        update = Update.de_json({"update_id": 202}, Bot("0:synthetic"))
        callback = AsyncMock(side_effect=RuntimeError("synthetic handler failure"))
        delays = []

        async def stop_during_sleep(delay):
            delays.append(delay)
            adapter._running = False

        monkeypatch.setattr(channel, "asyncio", SimpleNamespace(sleep=stop_during_sleep))
        try:
            await inbox.put(update)
            with pytest.raises(RuntimeError, match="synthetic handler failure"):
                await adapter._durable(callback)(await inbox.get(), None)
            inbox.task_done()
            assert callback.await_count == 1 and delays == [1]
            assert not inbox.processed(202) and not adapter._reached_publish
        finally:
            inbox.close()

    asyncio.run(run())
