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


# --- MessageJournal tests ---


class TestMessageJournal:
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
                await bus.request(env, timeout=0.01)

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
            assert received == [env]  # the whole envelope survives the journal

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
            for _ in range(200):  # until the pool has run it, then once more to see a duplicate
                if pool._run_worker.await_count:  # type: ignore[attr-defined]
                    break
                await asyncio.sleep(0.01)
            await asyncio.sleep(0)
            await pool.stop()
            return bus, pool._run_worker  # type: ignore[return-value]

        bus, run_worker = asyncio.run(_run())
        run_worker.assert_awaited_once()
        assert bus.pending_count("w1") == 0
        assert bus.journal.undelivered() == []
