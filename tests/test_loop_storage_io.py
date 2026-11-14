"""Durable storage may be slow without delaying the event loop."""

import asyncio
import hashlib
import os
import threading
import time
from pathlib import Path

import pytest

from looper.agents import knowledge
from looper.agents.bus import MessageBus
from looper.agents.message import MANAGER_ADDRESS, MessageEnvelope, MessageType, channel_address
from looper.db import project_documents


async def _responsive(operation):
    ticks = [time.monotonic()]

    async def heartbeat():
        while True:
            await asyncio.sleep(0.01)
            ticks.append(time.monotonic())

    pulse = asyncio.create_task(heartbeat())
    await asyncio.sleep(0)
    try:
        result = await operation
        await asyncio.sleep(0.01)
    finally:
        pulse.cancel()
        await asyncio.gather(pulse, return_exceptions=True)
    gap = max(b - a for a, b in zip(ticks, ticks[1:]))
    print(f"max loop gap: {gap:.3f}s")
    assert gap < 0.2
    return result


def _message(text):
    return MessageEnvelope.create(source=channel_address("web"), target=MANAGER_ADDRESS,
                                  message_type=MessageType.USER_MESSAGE, payload={"text": text})


def test_publish_waits_for_fsync_without_blocking_and_preserves_order(tmp_path, monkeypatch):
    async def run():
        bus = MessageBus(tmp_path)
        await bus.start()
        synced = []
        real_fsync = os.fsync

        def slow_fsync(fd):
            time.sleep(0.5)
            real_fsync(fd)
            synced.append(True)

        messages = [_message("first"), _message("second")]
        try:
            with monkeypatch.context() as patch:
                patch.setattr(os, "fsync", slow_fsync)

                async def publish():
                    first = asyncio.create_task(bus.publish(messages[0]))
                    second = asyncio.create_task(bus.publish(messages[1]))
                    await first
                    assert synced and bus.get_queue("manager").get_nowait() == messages[0]
                    await second
                    assert len(synced) == 2
                    assert bus.get_queue("manager").get_nowait() == messages[1]

                await _responsive(publish())
            assert bus.journal.undelivered() == messages
        finally:
            await bus.shutdown()

    asyncio.run(run())


def test_publish_fsync_error_does_not_deliver(tmp_path, monkeypatch):
    async def run():
        bus = MessageBus(tmp_path)
        await bus.start()
        try:
            with monkeypatch.context() as patch:
                def fail(fd):
                    raise OSError("disk failed")
                patch.setattr(os, "fsync", fail)
                with pytest.raises(OSError, match="disk failed"):
                    await bus.publish(_message("failed"))
            assert bus.get_queue("manager").empty()
        finally:
            await bus.shutdown()
    asyncio.run(run())


def test_start_does_not_replay_an_inflight_publish_twice(tmp_path, monkeypatch):
    async def run():
        bus = MessageBus(tmp_path)
        entered, release = threading.Event(), threading.Event()
        real_fsync = os.fsync
        loop_thread = threading.get_ident()
        message = _message("arrived during startup")

        def fsync(fd):
            if threading.get_ident() != loop_thread:
                entered.set()
                assert release.wait(5)
            real_fsync(fd)

        try:
            with monkeypatch.context() as patch:
                patch.setattr(os, "fsync", fsync)
                publisher = asyncio.create_task(bus.publish(message))
                assert await asyncio.to_thread(entered.wait, 5)
                starting = asyncio.create_task(bus.start())
                await asyncio.sleep(0.01)
                release.set()
                await asyncio.gather(publisher, starting)
            assert await bus.drain_queue("manager") == [message]
        finally:
            release.set()
            await bus.shutdown()

    asyncio.run(run())


def test_shutdown_waits_for_cancelled_publish_before_compacting(tmp_path, monkeypatch):
    async def run():
        bus = MessageBus(tmp_path)
        await bus.start()
        entered, release = threading.Event(), threading.Event()
        real_fsync = os.fsync
        message = _message("cancelled publisher")

        def fsync(fd):
            entered.set()
            assert release.wait(2)
            real_fsync(fd)

        try:
            with monkeypatch.context() as patch:
                patch.setattr(os, "fsync", fsync)
                publisher = asyncio.create_task(bus.publish(message))
                assert await asyncio.to_thread(entered.wait, 2)
                publisher.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await publisher
                stopping = asyncio.create_task(bus.shutdown())
                await asyncio.sleep(0.01)
                assert not stopping.done()
                release.set()
                await stopping
            assert bus.get_queue("manager").empty()
            assert bus.journal.undelivered() == [message]
        finally:
            release.set()
            await bus.shutdown()

    asyncio.run(run())


def test_knowledge_checks_off_loop_and_reuses_unchanged_copy(tmp_path, monkeypatch):
    content = "stored document"
    digest = hashlib.sha256(content.encode()).hexdigest()
    row = dict(id="doc", project_id="proj-1", source_kind="text", title="Guide", summary="",
               filename="Guide.md", size_bytes=len(content), sha256=digest)
    stored = knowledge.stored_set(tmp_path, "proj-1", [row])
    target = stored.directory / stored.names["doc"]
    knowledge.materialize(target, row, content)
    reads = []
    real_read = Path.read_bytes

    def slow_read(path):
        if path == target:
            reads.append(path)
            time.sleep(0.5)
        return real_read(path)

    async def rows(project_id):
        return [row]

    monkeypatch.setattr(project_documents, "list_documents", rows)
    monkeypatch.setattr(Path, "read_bytes", slow_read)

    async def run():
        result = await _responsive(knowledge.load_knowledge(tmp_path, {"id": "proj-1"}, str(tmp_path)))
        assert result.error is None and result.documents == 1
        assert reads == [target]
        assert await knowledge.load_knowledge(tmp_path, {"id": "proj-1"}, str(tmp_path)) == result
        assert reads == [target]

    asyncio.run(run())


def test_knowledge_metadata_cache_detects_same_size_change_and_replacement(tmp_path):
    path = tmp_path / "copy"
    path.write_bytes(b"good")
    expected = hashlib.sha256(b"good").hexdigest()
    assert knowledge._is_materialized(path, expected)
    before = path.stat()
    path.write_bytes(b"evil")
    os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
    assert not knowledge._is_materialized(path, expected)
    replacement = tmp_path / "replacement"
    replacement.write_bytes(b"good")
    replacement.replace(path)
    assert knowledge._is_materialized(path, expected)
    path.unlink()
    assert not knowledge._is_materialized(path, expected)
