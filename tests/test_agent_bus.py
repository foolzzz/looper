"""Tests for the agent message bus and journal."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from loopx.agents.bus import MessageBus, MessageJournal
from loopx.agents.message import (
    MANAGER_ADDRESS,
    PLANNER_ADDRESS,
    AgentAddress,
    MessageEnvelope,
    MessageType,
    channel_address,
    planner_address,
    worker_address,
)


def _make_envelope(
    *,
    source: AgentAddress = channel_address("web"),
    target: AgentAddress = MANAGER_ADDRESS,
    message_type: MessageType = MessageType.USER_MESSAGE,
    payload: dict | None = None,
    correlation_id: str | None = None,
    reply_to: str | None = None,
) -> MessageEnvelope:
    return MessageEnvelope.create(
        source=source,
        target=target,
        message_type=message_type,
        payload=payload or {"text": "hello"},
        correlation_id=correlation_id,
        reply_to=reply_to,
    )


# --- MessageEnvelope tests ---


class TestMessageEnvelope:
    def test_roundtrip_serialization(self) -> None:
        env = _make_envelope()
        d = env.to_dict()
        restored = MessageEnvelope.from_dict(d)
        assert restored.message_id == env.message_id
        assert restored.correlation_id == env.correlation_id
        assert restored.source == env.source
        assert restored.target == env.target
        assert restored.message_type == env.message_type
        assert restored.payload == env.payload

    def test_create_generates_unique_ids(self) -> None:
        a = _make_envelope()
        b = _make_envelope()
        assert a.message_id != b.message_id
        assert a.correlation_id != b.correlation_id

    def test_create_with_explicit_correlation(self) -> None:
        env = _make_envelope(correlation_id="shared-123")
        assert env.correlation_id == "shared-123"

    def test_create_with_reply_to(self) -> None:
        original = _make_envelope()
        reply = _make_envelope(reply_to=original.message_id)
        assert reply.reply_to == original.message_id

    def test_message_type_serialization(self) -> None:
        for mt in MessageType:
            env = _make_envelope(message_type=mt)
            d = env.to_dict()
            assert d["message_type"] == mt.value
            restored = MessageEnvelope.from_dict(d)
            assert restored.message_type == mt


class TestAgentAddress:
    def test_channel_address(self) -> None:
        addr = channel_address("telegram")
        assert addr.channel == "telegram"
        assert addr.agent_type == "channel"

    def test_worker_address(self) -> None:
        addr = worker_address("worker-abc123")
        assert addr.agent_type == "worker"
        assert addr.agent_id == "worker-abc123"
        assert addr.channel is None

    def test_roundtrip(self) -> None:
        addr = AgentAddress(agent_type="planner", agent_id="planner", channel=None)
        d = addr.to_dict()
        assert "channel" not in d
        restored = AgentAddress.from_dict(d)
        assert restored == addr


# --- MessageJournal tests ---


class TestMessageJournal:
    def test_append_and_undelivered(self, tmp_path: Path) -> None:
        journal = MessageJournal(tmp_path / "bus")
        env = _make_envelope()
        journal.append(env)
        undelivered = journal.undelivered()
        assert len(undelivered) == 1
        assert undelivered[0].message_id == env.message_id

    def test_mark_delivered(self, tmp_path: Path) -> None:
        journal = MessageJournal(tmp_path / "bus")
        env = _make_envelope()
        journal.append(env)
        journal.mark_delivered(env.message_id)
        assert journal.undelivered() == []

    def test_compact(self, tmp_path: Path) -> None:
        journal = MessageJournal(tmp_path / "bus")
        env1 = _make_envelope()
        env2 = _make_envelope()
        journal.append(env1)
        journal.append(env2)
        journal.mark_delivered(env1.message_id)
        journal.compact()

        journal2 = MessageJournal(tmp_path / "bus")
        undelivered = journal2.undelivered()
        assert len(undelivered) == 1
        assert undelivered[0].message_id == env2.message_id

    def test_corrupt_entry_skipped(self, tmp_path: Path) -> None:
        bus_dir = tmp_path / "bus"
        bus_dir.mkdir()
        journal_path = bus_dir / "journal.jsonl"
        env = _make_envelope()
        journal_path.write_text(
            "not-json\n" + json.dumps(env.to_dict()) + "\n",
            encoding="utf-8",
        )
        journal = MessageJournal(bus_dir)
        undelivered = journal.undelivered()
        assert len(undelivered) == 1

    def test_crash_recovery(self, tmp_path: Path) -> None:
        journal = MessageJournal(tmp_path / "bus")
        envs = [_make_envelope() for _ in range(3)]
        for env in envs:
            journal.append(env)
        journal.mark_delivered(envs[0].message_id)

        journal2 = MessageJournal(tmp_path / "bus")
        undelivered = journal2.undelivered()
        assert len(undelivered) == 2
        ids = {e.message_id for e in undelivered}
        assert envs[1].message_id in ids
        assert envs[2].message_id in ids


# --- MessageBus tests ---


class TestMessageBus:
    def test_publish_and_subscribe(self, tmp_path: Path) -> None:
        async def _run() -> None:
            bus = MessageBus(tmp_path)
            await bus.start()
            env = _make_envelope(target=MANAGER_ADDRESS)
            await bus.publish(env)
            received = await bus.drain_queue("manager")
            assert len(received) == 1
            assert received[0].message_id == env.message_id

        asyncio.run(_run())

    def test_subscribe_iterator(self, tmp_path: Path) -> None:
        async def _run() -> None:
            bus = MessageBus(tmp_path)
            await bus.start()
            env = _make_envelope(target=MANAGER_ADDRESS)

            collected: list[MessageEnvelope] = []

            async def consumer() -> None:
                async for msg in bus.subscribe("manager"):
                    collected.append(msg)
                    if len(collected) >= 2:
                        break

            task = asyncio.create_task(consumer())
            await asyncio.sleep(0.01)
            await bus.publish(env)
            env2 = _make_envelope(target=MANAGER_ADDRESS)
            await bus.publish(env2)
            await asyncio.wait_for(task, timeout=2.0)
            assert len(collected) == 2

        asyncio.run(_run())

    def test_request_reply(self, tmp_path: Path) -> None:
        async def _run() -> None:
            bus = MessageBus(tmp_path)
            await bus.start()
            request_env = _make_envelope(
                source=MANAGER_ADDRESS,
                target=PLANNER_ADDRESS,
                message_type=MessageType.TASK_REQUEST,
            )

            async def responder() -> None:
                q = bus.get_queue("planner")
                msg = await q.get()
                reply = MessageEnvelope.create(
                    source=PLANNER_ADDRESS,
                    target=MANAGER_ADDRESS,
                    message_type=MessageType.TASK_ACCEPTED,
                    payload={"plan_id": "plan-1"},
                    reply_to=msg.message_id,
                    correlation_id=msg.correlation_id,
                )
                await bus.publish(reply)

            task = asyncio.create_task(responder())
            result = await asyncio.wait_for(
                bus.request(request_env, timeout=2.0),
                timeout=3.0,
            )
            assert result.message_type == MessageType.TASK_ACCEPTED
            assert result.payload["plan_id"] == "plan-1"
            await task

        asyncio.run(_run())

    def test_request_timeout(self, tmp_path: Path) -> None:
        async def _run() -> None:
            bus = MessageBus(tmp_path)
            await bus.start()
            env = _make_envelope(target=PLANNER_ADDRESS)
            with pytest.raises(asyncio.TimeoutError):
                await bus.request(env, timeout=0.1)

        asyncio.run(_run())

    def test_topic_subscriber(self, tmp_path: Path) -> None:
        async def _run() -> None:
            bus = MessageBus(tmp_path)
            await bus.start()
            observed: list[MessageEnvelope] = []
            bus.subscribe_topic(MessageType.USER_MESSAGE, observed.append)
            env = _make_envelope(message_type=MessageType.USER_MESSAGE)
            await bus.publish(env)
            assert len(observed) == 1
            assert observed[0].message_id == env.message_id

        asyncio.run(_run())

    def test_unsubscribe_topic(self, tmp_path: Path) -> None:
        async def _run() -> None:
            bus = MessageBus(tmp_path)
            await bus.start()
            observed: list[MessageEnvelope] = []
            bus.subscribe_topic(MessageType.USER_MESSAGE, observed.append)
            bus.unsubscribe_topic(MessageType.USER_MESSAGE, observed.append)
            env = _make_envelope(message_type=MessageType.USER_MESSAGE)
            await bus.publish(env)
            assert len(observed) == 0

        asyncio.run(_run())

    def test_pending_count(self, tmp_path: Path) -> None:
        async def _run() -> None:
            bus = MessageBus(tmp_path)
            await bus.start()
            assert bus.pending_count("manager") == 0
            await bus.publish(_make_envelope(target=MANAGER_ADDRESS))
            assert bus.pending_count("manager") == 1

        asyncio.run(_run())

    def test_journal_persistence(self, tmp_path: Path) -> None:
        async def _run() -> None:
            bus1 = MessageBus(tmp_path)
            await bus1.start()
            env = _make_envelope(target=MANAGER_ADDRESS)
            await bus1.publish(env)
            await bus1.shutdown()

            bus2 = MessageBus(tmp_path)
            await bus2.start()
            received = await bus2.drain_queue("manager")
            assert len(received) == 1
            assert received[0].message_id == env.message_id

        asyncio.run(_run())

    def test_messages_not_cross_delivered(self, tmp_path: Path) -> None:
        async def _run() -> None:
            bus = MessageBus(tmp_path)
            await bus.start()
            env_mgr = _make_envelope(target=MANAGER_ADDRESS)
            env_planner = _make_envelope(target=PLANNER_ADDRESS)
            await bus.publish(env_mgr)
            await bus.publish(env_planner)

            mgr_msgs = await bus.drain_queue("manager")
            planner_msgs = await bus.drain_queue("planner")
            assert len(mgr_msgs) == 1
            assert mgr_msgs[0].message_id == env_mgr.message_id
            assert len(planner_msgs) == 1
            assert planner_msgs[0].message_id == env_planner.message_id

        asyncio.run(_run())

    def test_shutdown_compacts(self, tmp_path: Path) -> None:
        async def _run() -> None:
            bus = MessageBus(tmp_path)
            await bus.start()
            env = _make_envelope(target=MANAGER_ADDRESS)
            await bus.publish(env)
            await bus.drain_queue("manager")
            await bus.shutdown()

            bus2 = MessageBus(tmp_path)
            await bus2.start()
            received = await bus2.drain_queue("manager")
            assert len(received) == 0

        asyncio.run(_run())


class TestTopicConsumers:
    def test_consumer_takes_delivery(self, tmp_path: Path) -> None:
        async def _run() -> None:
            bus = MessageBus(tmp_path)
            await bus.start()
            taken: list[MessageEnvelope] = []
            observed: list[MessageEnvelope] = []
            bus.subscribe_topic(MessageType.WORK_ITEM, lambda e: taken.append(e) or True, consumes=True)
            bus.subscribe_topic(MessageType.WORK_ITEM, observed.append)
            env = _make_envelope(target=worker_address("w1"), message_type=MessageType.WORK_ITEM)
            await bus.publish(env)
            assert [e.message_id for e in taken] == [env.message_id]
            assert [e.message_id for e in observed] == [env.message_id]
            assert bus.pending_count("w1") == 0  # not left in a queue nobody reads
            assert bus.journal.undelivered() == []

        asyncio.run(_run())

    def test_declined_message_stays_queued(self, tmp_path: Path) -> None:
        async def _run() -> None:
            bus = MessageBus(tmp_path)
            await bus.start()
            bus.subscribe_topic(MessageType.WORK_ITEM, lambda e: False, consumes=True)
            env = _make_envelope(target=worker_address("w1"), message_type=MessageType.WORK_ITEM)
            await bus.publish(env)
            assert bus.pending_count("w1") == 1
            assert [e.message_id for e in bus.journal.undelivered()] == [env.message_id]

        asyncio.run(_run())

    def test_unsubscribe_consumer(self, tmp_path: Path) -> None:
        async def _run() -> None:
            bus = MessageBus(tmp_path)
            await bus.start()
            consumer = lambda e: True  # noqa: E731
            bus.subscribe_topic(MessageType.WORK_ITEM, consumer, consumes=True)
            bus.unsubscribe_topic(MessageType.WORK_ITEM, consumer)
            await bus.publish(_make_envelope(target=worker_address("w1"), message_type=MessageType.WORK_ITEM))
            assert bus.pending_count("w1") == 1

        asyncio.run(_run())


class TestReplay:
    def test_messages_for_per_task_agents_are_dropped(self, tmp_path: Path) -> None:
        """Planners and Workers do not survive a restart; their tasks are stopped."""
        journal = MessageJournal(tmp_path / ".local" / "agent-bus")
        for_manager = _make_envelope(target=MANAGER_ADDRESS)
        stale = [
            _make_envelope(target=worker_address("w1"), message_type=MessageType.WORK_ITEM),
            _make_envelope(target=planner_address("t1"), message_type=MessageType.WORK_RESULT),
            _make_envelope(target=PLANNER_ADDRESS, message_type=MessageType.WORK_RESULT),
        ]
        for env in [for_manager, *stale]:
            journal.append(env)

        async def _run() -> MessageBus:
            bus = MessageBus(tmp_path)
            await bus.start()
            return bus

        bus = asyncio.run(_run())
        assert bus.pending_count("manager") == 1
        assert bus.pending_count("w1") == 0
        assert bus.pending_count("planner-t1") == 0
        assert bus.pending_count("planner") == 0
        # Only the replayed message remains in the journal; the stale ones are gone.
        lines = (tmp_path / ".local" / "agent-bus" / "journal.jsonl").read_text().splitlines()
        assert [json.loads(line)["message_id"] for line in lines] == [for_manager.message_id]
        assert [e.message_id for e in MessageJournal(tmp_path / ".local" / "agent-bus").undelivered()] == [
            for_manager.message_id
        ]


class TestWorkerPoolConsumesWork:
    def test_dispatched_work_is_delivered_once(self, tmp_path: Path) -> None:
        from unittest.mock import AsyncMock

        from loopx.agents.tiers.worker import WorkerConfig, WorkerPool

        async def _run() -> tuple[MessageBus, AsyncMock]:
            bus = MessageBus(tmp_path)
            await bus.start()
            pool = WorkerPool(config=WorkerConfig(), runtime_root=tmp_path)
            pool._run_worker = AsyncMock()  # type: ignore[method-assign]
            await pool.start(bus)
            await asyncio.sleep(0)  # let the listener register its topics
            await bus.publish(_make_envelope(
                source=planner_address("t1"),
                target=worker_address("w1"),
                message_type=MessageType.WORK_ITEM,
                payload={"task_id": "s1"},
            ))
            await asyncio.sleep(0.05)
            await pool.stop()
            return bus, pool._run_worker  # type: ignore[return-value]

        bus, run_worker = asyncio.run(_run())
        run_worker.assert_awaited_once()
        assert bus.pending_count("w1") == 0
        assert bus.journal.undelivered() == []
