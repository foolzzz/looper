"""Startup keeps replayed Manager turns durable until their platform tools are listening."""

import asyncio

import pytest

from looper.agents.bus import MessageBus
from looper.agents.message import MANAGER_ADDRESS, MessageEnvelope, MessageType, channel_address
from looper.agents.tiers.manager import ManagerAgent, ManagerConfig
from tests.db_support import until
from tests.test_manager_stop import _Session


@pytest.mark.parametrize("release", [False, True], ids=["stop-before-ready", "answer-after-ready"])
def test_replayed_turn_waits_for_tools_without_acknowledging(agent_db, tmp_path, monkeypatch, release):
    async def run():
        bus = MessageBus(runtime_root=tmp_path)
        question = MessageEnvelope.create(
            source=channel_address("web"), target=MANAGER_ADDRESS, message_type=MessageType.USER_MESSAGE,
            payload={"text": "held during drain"})
        await bus.publish(question)
        await bus.shutdown()
        bus = MessageBus(runtime_root=tmp_path)
        await bus.start()
        ready, waiting = asyncio.Event(), asyncio.Event()
        wait = ready.wait

        async def wait_for_tools():
            waiting.set()
            await wait()

        monkeypatch.setattr(ready, "wait", wait_for_tools)
        manager = ManagerAgent(config=ManagerConfig(work_dir=tmp_path), runtime_root=tmp_path, tools_ready=ready)
        manager._claude = _Session(hold=False)
        await manager.start(bus)
        try:
            await asyncio.wait_for(waiting.wait(), 2)
            assert [entry.message_id for entry in bus.journal.undelivered()] == [question.message_id]
            assert bus.pending_count("manager") == 1
            assert manager._claude.turns == []
            assert bus.pending_count("channel-web") == 0
            if release:
                ready.set()
                await until(lambda: question.message_id not in {e.message_id for e in bus.journal.undelivered()},
                            timeout=5)
                assert len(manager._claude.turns) == 1
                replies = [e for e in await bus.drain_queue("channel-web")
                           if e.message_type is MessageType.AGENT_REPLY]
                assert len(replies) == 1 and "error" not in replies[0].payload
        finally:
            await manager.stop()
            await bus.shutdown()
        if not release:
            assert [entry.message_id for entry in bus.journal.undelivered()] == [question.message_id]
            assert manager._claude.turns == []

    asyncio.run(asyncio.wait_for(run(), 10))
