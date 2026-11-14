"""Durable channel storage must leave the event loop free while SQLite commits."""

import asyncio
import json
import sqlite3
import threading
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from looper.agents.channels.lark import LarkChannelAdapter
from looper.agents.channels.allowlist import SenderAllowlist
from looper.agents.channels.lark_cli import RECEIVE_EVENT
from looper.agents.channels.lark_inbox import LarkInbox
from looper.agents.channels.telegram_inbox import TelegramInbox
from looper.agents.channels.telegram import TelegramChannelAdapter
from looper.agents.channels.registry import ChannelRegistry


async def loop_gap(operation):
    gaps = []
    done = False

    async def heartbeat():
        previous = time.monotonic()
        while not done:
            await asyncio.sleep(0.01)
            now = time.monotonic()
            gaps.append(now - previous)
            previous = now

    ticker = asyncio.create_task(heartbeat())
    await asyncio.sleep(0)
    try:
        await operation()
        await asyncio.sleep(0.02)
    finally:
        done = True
        await ticker
    return max(gaps)


@pytest.mark.parametrize("channel", ["lark_event", "lark_heartbeat", "lark_thread", "telegram", "telegram_stop"])
def test_channel_commits_do_not_block_loop(tmp_path, monkeypatch, channel):
    connect = sqlite3.connect
    threads = set()
    committed = threading.Event()

    class SlowConnection(sqlite3.Connection):
        def __exit__(self, *args):
            threads.add(threading.get_ident())
            time.sleep(0.5)
            result = super().__exit__(*args)
            committed.set()
            return result

    monkeypatch.setattr(sqlite3, "connect", lambda *args, **kwargs: connect(*args, factory=SlowConnection, **kwargs))

    async def run():
        inbox = (LarkInbox if channel.startswith("lark") else TelegramInbox)(tmp_path / "inbox.sqlite3")
        try:
            if channel.startswith("lark"):
                adapter = LarkChannelAdapter(
                    runtime_root=tmp_path, allowlist=SenderAllowlist("channel-lark", frozenset({"synthetic"})))
                adapter._inbox = inbox
                if channel == "lark_event":
                    adapter._process_lark_event = AsyncMock(return_value=False)

                    async def operation():
                        await adapter._handle_lark_event({"type": RECEIVE_EVENT, "message_id": "synthetic"})
                elif channel == "lark_heartbeat":
                    ready = asyncio.Event()
                    ready.set()
                    adapter._transport = SimpleNamespace(consumer_started=ready)
                    adapter._running = True
                    checkpoint = inbox.checkpoint

                    def stop_after_checkpoint(at):
                        checkpoint(at)
                        adapter._running = False

                    monkeypatch.setattr(inbox, "checkpoint", stop_after_checkpoint)
                    monkeypatch.setattr("looper.agents.channels.lark._HEARTBEAT_SECONDS", 0.01)
                    operation = adapter._heartbeat
                else:
                    adapter._transport.on_thread_activity = adapter._remember_thread
                    monkeypatch.setattr("looper.agents.channels.lark_cli.run_json", AsyncMock(return_value={
                        "data": {"chat_id": "synthetic", "thread_id": "synthetic-thread", "message_id": "sent"}}))

                    async def operation():
                        await adapter._transport.send(chat_id="synthetic", text="Synthetic reply", return_id=True)
                        assert committed.is_set(), "send returned before thread activity was durable"

            elif channel == "telegram_stop":
                adapter = TelegramChannelAdapter(bot_token="0:synthetic", allowlist=SenderAllowlist(
                    "channel-telegram", frozenset({"7"})))
                adapter._inbox = inbox
                adapter._app = SimpleNamespace(updater=SimpleNamespace(running=True))
                registry = ChannelRegistry(None)
                registry._channels[adapter.channel_id] = adapter

                async def operation():
                    registry.prepare_stop_all()
                    await registry.wait_prepared_stop_all()
                    assert committed.is_set(), "agent drain started before its boundary was durable"
            else:
                update = SimpleNamespace(update_id=1, to_json=lambda: json.dumps({"update_id": 1}))
                async def operation():
                    await inbox.put(update)
            gap = await loop_gap(operation)
            assert threads and threading.get_ident() not in threads
            assert gap < 0.2, f"event loop stalled for {gap:.3f}s"
            print(f"{channel} max_loop_gap_ms={gap * 1000:.1f}")
        finally:
            inbox.close()

    asyncio.run(asyncio.wait_for(run(), 5))


@pytest.mark.parametrize("inbox_type", [LarkInbox, TelegramInbox])
def test_channel_storage_open_and_close_do_not_block_loop(tmp_path, monkeypatch, inbox_type):
    connect = sqlite3.connect
    threads = set()

    class SlowConnection(sqlite3.Connection):
        def commit(self):
            threads.add(threading.get_ident())
            time.sleep(0.5)
            return super().commit()

        def close(self):
            threads.add(threading.get_ident())
            time.sleep(0.5)
            return super().close()

    monkeypatch.setattr(sqlite3, "connect", lambda *args, **kwargs: connect(*args, factory=SlowConnection, **kwargs))

    async def run():
        async def lifecycle():
            inbox = await inbox_type.open(tmp_path / "inbox.sqlite3")
            await inbox.aclose()

        assert await loop_gap(lifecycle) < 0.2
        assert len(threads) == 1 and threading.get_ident() not in threads

    asyncio.run(asyncio.wait_for(run(), 5))


@pytest.mark.parametrize("failure", ["checkpoint", "transport"])
def test_lark_stop_closes_storage_after_cleanup_failure(tmp_path, monkeypatch, failure):
    connect = sqlite3.connect
    closed = threading.Event()

    class Connection(sqlite3.Connection):
        def close(self):
            result = super().close()
            closed.set()
            return result

    monkeypatch.setattr(sqlite3, "connect", lambda *args, **kwargs: connect(*args, factory=Connection, **kwargs))

    async def run():
        inbox = await LarkInbox.open(tmp_path / "inbox.sqlite3")
        ready = asyncio.Event()
        ready.set()
        adapter = LarkChannelAdapter(
            runtime_root=tmp_path, allowlist=SenderAllowlist("channel-lark", frozenset({"synthetic"})),
            transport=SimpleNamespace(consumer_started=ready, aclose=AsyncMock()))
        adapter._inbox = inbox

        def failed_checkpoint(at):
            raise OSError("synthetic checkpoint failure")

        if failure == "checkpoint":
            monkeypatch.setattr(inbox, "checkpoint", failed_checkpoint)
        else:
            adapter._transport.aclose.side_effect = OSError("synthetic checkpoint failure")
        try:
            with pytest.raises(OSError, match="synthetic checkpoint failure"):
                await adapter.stop()
            assert closed.is_set()
            assert adapter._inbox is None
        finally:
            await inbox.aclose()

    asyncio.run(asyncio.wait_for(run(), 5))
