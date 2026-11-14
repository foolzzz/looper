"""Durable writes must not stall the event loop or acknowledge before commit."""

import asyncio
import os
import sqlite3
import threading
import time

import pytest

from looper.agents.bus import MessageBus
from looper.agents.channels.lark_inbox import LarkInbox
from looper.agents.channels.telegram_inbox import TelegramInbox
from looper.agents.message import MANAGER_ADDRESS, MessageEnvelope, MessageType, channel_address


async def _ticks_during(operation):
    gaps = []
    running = True

    async def tick():
        previous = time.monotonic()
        while running:
            await asyncio.sleep(.01)
            now = time.monotonic()
            gaps.append(now - previous)
            previous = now

    ticker = asyncio.create_task(tick())
    await asyncio.sleep(.02)
    try:
        await operation()
    finally:
        running = False
        await ticker
    assert max(gaps) < .2, f"event loop stalled for {max(gaps):.3f}s"


def test_publish_waits_for_fsync_without_blocking_loop(tmp_path, monkeypatch):
    async def run():
        bus = MessageBus(tmp_path)
        await bus.start()
        synced = False
        entered = asyncio.Event()
        loop = asyncio.get_running_loop()
        real_fsync = os.fsync

        def slow_fsync(fd):
            nonlocal synced
            loop.call_soon_threadsafe(entered.set)
            time.sleep(.5)
            real_fsync(fd)
            synced = True

        message = MessageEnvelope.create(source=channel_address("web"), target=MANAGER_ADDRESS,
                                         message_type=MessageType.USER_MESSAGE, payload={"text": "hello"})
        try:
            with monkeypatch.context() as patch:
                patch.setattr(os, "fsync", slow_fsync)
                async def publish():
                    pending = asyncio.create_task(bus.publish(message))
                    await asyncio.wait_for(entered.wait(), 2)
                    assert not pending.done() and bus.get_queue("manager").empty()
                    await pending

                await _ticks_during(publish)
                assert synced
                assert bus.get_queue("manager").get_nowait() == message
        finally:
            await bus.shutdown()

    asyncio.run(run())


@pytest.mark.parametrize("kind", ["lark", "telegram"])
def test_inbox_commit_keeps_loop_ticking(tmp_path, monkeypatch, kind):
    real_connect = sqlite3.connect
    commits = []
    threads = []

    class SlowConnection(sqlite3.Connection):
        def execute(self, *args, **kwargs):
            threads.append(threading.get_ident())
            return super().execute(*args, **kwargs)

        def close(self):
            threads.append(threading.get_ident())
            return super().close()

        def commit(self):
            threads.append(threading.get_ident())
            time.sleep(.5)
            commits.append(True)
            return super().commit()

        def __exit__(self, *args):
            # sqlite's C context manager bypasses Python's commit override.
            if args[0] is None:
                self.commit()
            else:
                self.rollback()
            return False

    def connect(*args, **kwargs):
        threads.append(threading.get_ident())
        return real_connect(*args, factory=SlowConnection, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", connect)

    async def run():
        inbox = await (LarkInbox if kind == "lark" else TelegramInbox).open(tmp_path / "inbox.sqlite3")
        commits.clear()
        try:
            async def write():
                if kind == "lark":
                    await inbox.checkpoint(123)
                else:
                    await inbox.polling_stopped(123)
            await _ticks_during(write)
            assert commits == [True]
        finally:
            await inbox.close()
        assert len(set(threads)) == 1
        assert threading.get_ident() not in threads

    asyncio.run(run())


def test_cancelled_append_is_drained_before_replay_and_shutdown(tmp_path, monkeypatch):
    async def run():
        bus = MessageBus(tmp_path)
        entered, release = asyncio.Event(), threading.Event()
        loop = asyncio.get_running_loop()
        real_fsync = os.fsync
        first = MessageEnvelope.create(source=channel_address("web"), target=MANAGER_ADDRESS,
                                       message_type=MessageType.USER_MESSAGE, payload={"text": "first"})
        second = MessageEnvelope.create(source=channel_address("web"), target=MANAGER_ADDRESS,
                                        message_type=MessageType.USER_MESSAGE, payload={"text": "second"})

        def blocked_fsync(fd):
            loop.call_soon_threadsafe(entered.set)
            assert release.wait(3), "test did not release fsync"
            real_fsync(fd)

        pending = None
        try:
            with monkeypatch.context() as patch:
                patch.setattr(os, "fsync", blocked_fsync)
                pending = asyncio.create_task(bus.publish(first))
                await asyncio.wait_for(entered.wait(), 2)
                pending.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await pending
                # Starting now must wait for the cancelled publisher's I/O,
                # then replay its durable message exactly once.
                starting = asyncio.create_task(bus.start())
                await asyncio.sleep(.02)
                assert not starting.done()
                release.set()
                await starting
                await bus.publish(second)
            assert [bus.get_queue("manager").get_nowait().message_id for _ in range(2)] == [
                first.message_id, second.message_id]
            await bus.shutdown()
            restarted = MessageBus(tmp_path)
            try:
                await restarted.start()
                assert [restarted.get_queue("manager").get_nowait().message_id for _ in range(2)] == [
                    first.message_id, second.message_id]
                assert restarted.get_queue("manager").empty()
            finally:
                await restarted.shutdown()
        finally:
            release.set()
            if pending is not None:
                await asyncio.gather(pending, return_exceptions=True)
            await bus.shutdown()

    asyncio.run(run())


@pytest.mark.parametrize("kind", ["lark", "telegram"])
def test_failed_commit_is_not_acknowledged_and_can_be_retried(tmp_path, monkeypatch, kind):
    from telegram import Update

    real_connect = sqlite3.connect
    fail = False

    class FailingConnection(sqlite3.Connection):
        def __exit__(self, *args):
            if fail and args[0] is None:
                self.rollback()
                raise sqlite3.OperationalError("synthetic commit failure")
            return super().__exit__(*args)

    monkeypatch.setattr(sqlite3, "connect", lambda *args, **kwargs:
                        real_connect(*args, factory=FailingConnection, **kwargs))

    async def run():
        nonlocal fail
        inbox = await (LarkInbox if kind == "lark" else TelegramInbox).open(tmp_path / "inbox.sqlite3")
        update = Update.de_json({"update_id": 1}, None)
        try:
            fail = True
            with pytest.raises(sqlite3.OperationalError, match="synthetic commit failure"):
                if kind == "lark":
                    await inbox.checkpoint(123)
                else:
                    await inbox.put(update)
            if kind == "lark":
                assert inbox.boundary is None
            else:
                assert inbox.empty()
            fail = False
            if kind == "lark":
                await inbox.checkpoint(456)
                assert inbox.boundary == 456
            else:
                await inbox.put(update)
                assert inbox.get_nowait().update_id == 1
                inbox.task_done()
        finally:
            await inbox.close()

    asyncio.run(run())


@pytest.mark.parametrize("kind", ["lark", "telegram"])
def test_inbox_close_drains_a_cancelled_commit(tmp_path, monkeypatch, kind):
    async def run():
        cls = LarkInbox if kind == "lark" else TelegramInbox
        path = tmp_path / "inbox.sqlite3"
        inbox = await cls.open(path)
        entered, release = asyncio.Event(), threading.Event()
        loop = asyncio.get_running_loop()
        real_connect = sqlite3.connect
        # Reopen using a real thread-affine connection with a controllable commit.
        await inbox.close()

        class BlockedConnection(sqlite3.Connection):
            def __exit__(self, *args):
                loop.call_soon_threadsafe(entered.set)
                assert release.wait(3), "test did not release commit"
                return super().__exit__(*args)

        monkeypatch.setattr(sqlite3, "connect", lambda *args, **kwargs:
                            real_connect(*args, factory=BlockedConnection, **kwargs))
        inbox = await cls.open(path)
        closing = None
        try:
            writing = asyncio.ensure_future(inbox.checkpoint(123) if kind == "lark" else inbox.polling_stopped(123))
            await asyncio.wait_for(entered.wait(), 2)
            writing.cancel()
            with pytest.raises(asyncio.CancelledError):
                await writing
            closing = asyncio.create_task(inbox.close())
            await asyncio.sleep(.02)
            assert not closing.done()
            release.set()
            await closing
            reopened = await cls.open(path)
            try:
                assert (reopened.boundary if kind == "lark" else reopened.previous_polling_stopped_at) == 123
            finally:
                await reopened.close()
        finally:
            release.set()
            if closing is not None:
                await closing
            await inbox.close()

    asyncio.run(run())


def test_publish_fsync_failure_is_propagated_before_delivery(tmp_path, monkeypatch):
    async def run():
        bus = MessageBus(tmp_path)
        message = MessageEnvelope.create(source=channel_address("web"), target=MANAGER_ADDRESS,
                                         message_type=MessageType.USER_MESSAGE, payload={"text": "hello"})

        def fail(fd):
            raise OSError("synthetic fsync failure")

        try:
            with monkeypatch.context() as patch:
                patch.setattr(os, "fsync", fail)
                with pytest.raises(OSError, match="synthetic fsync failure"):
                    await bus.publish(message)
            assert bus.get_queue("manager").empty()
        finally:
            await bus.shutdown()

    asyncio.run(run())


def test_concurrent_publishes_keep_delivery_and_replay_order(tmp_path):
    async def run():
        bus = MessageBus(tmp_path)
        messages = [MessageEnvelope.create(source=channel_address("web"), target=MANAGER_ADDRESS,
                                          message_type=MessageType.USER_MESSAGE, payload={"text": text})
                    for text in ("first", "second", "third")]
        try:
            await asyncio.gather(*(bus.publish(message) for message in messages))
            assert [bus.get_queue("manager").get_nowait() for _ in messages] == messages
        finally:
            await bus.shutdown()
        restarted = MessageBus(tmp_path)
        try:
            await restarted.start()
            assert [restarted.get_queue("manager").get_nowait() for _ in messages] == messages
        finally:
            await restarted.shutdown()

    asyncio.run(run())
