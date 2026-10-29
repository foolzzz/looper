"""Tests for the agent message bus and journal."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from looper.agents.bus import MessageBus, MessageJournal
from looper.agents.message import (
    MANAGER_ADDRESS,
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
    @pytest.mark.parametrize("broken", ["not-json", "[]", '{"message_id": "x"}',
                                       json.dumps({**_make_envelope().to_dict(),
                                                   "source": {"agent_type": "channel", "agent_id": "x", "channel": 7}})])
    def test_corrupt_entries_refuse_replay_and_compaction(self, tmp_path: Path, broken: str) -> None:
        journal = MessageJournal(tmp_path / "bus")
        journal.append(_make_envelope())
        with journal._journal_path.open("a") as stream:
            stream.write(broken + "\n")
        before = journal._journal_path.read_bytes()
        for operation in (journal.transition, journal.undelivered, journal.compact):
            with pytest.raises(ValueError, match="invalid journal envelope.*:2:"):
                operation()
            assert journal._journal_path.read_bytes() == before

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
                target=planner_address("t1"),
                message_type=MessageType.TASK_REQUEST,
            )

            async def responder() -> None:
                q = bus.get_queue("planner-t1")
                msg = await q.get()
                reply = MessageEnvelope.create(
                    source=planner_address("t1"),
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

    def test_a_requester_stopped_as_its_reply_arrives_stays_stopped(self, tmp_path: Path) -> None:
        """A stop that cancels a request just as its reply comes in is not lost (Python 3.11's asyncio.wait_for
        returned the reply then, and a stop waiting for the requester waited for good)."""

        async def _run() -> asyncio.Task[MessageEnvelope]:
            bus = MessageBus(tmp_path)
            await bus.start()
            requester = asyncio.create_task(bus.request(_make_envelope(
                source=MANAGER_ADDRESS, target=planner_address("t1"), message_type=MessageType.TASK_REQUEST), timeout=5))
            asked = await bus.get_queue("planner-t1").get()
            await bus.publish(MessageEnvelope.create(
                source=planner_address("t1"), target=MANAGER_ADDRESS, message_type=MessageType.TASK_ACCEPTED, payload={},
                reply_to=asked.message_id))
            requester.cancel()  # the stop, as the reply wakes the requester
            await asyncio.wait([requester])
            return requester

        assert asyncio.run(_run()).cancelled()

    def test_request_timeout(self, tmp_path: Path) -> None:
        async def _run() -> None:
            bus = MessageBus(tmp_path)
            await bus.start()
            env = _make_envelope(target=planner_address("t1"))
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

    def test_a_status_update_is_not_journaled(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        # A status (a streamed delta, a phase) is progress of a turn that a restart ends: replaying it would show a
        # stale line. Journaling one cost a synchronous fsync on the event loop for each streamed delta.
        async def _run() -> None:
            bus1 = MessageBus(tmp_path)
            await bus1.start()
            synced: list[str] = []
            real_append = bus1.journal.append
            monkeypatch.setattr(bus1.journal, "append", lambda e: (synced.append(e.message_type), real_append(e)))
            status = _make_envelope(source=MANAGER_ADDRESS, target=channel_address("web"),
                                    message_type=MessageType.STATUS_UPDATE, payload={"text": "x", "delta": True})
            reply = _make_envelope(source=MANAGER_ADDRESS, target=channel_address("web"),
                                   message_type=MessageType.AGENT_REPLY, payload={"text": "done"})
            await bus1.publish(status)
            assert bus1.get_queue(channel_address("web").agent_id).get_nowait() == status
            await bus1.publish(reply)
            assert synced == [MessageType.AGENT_REPLY]
            await bus1.shutdown()

            bus2 = MessageBus(tmp_path)
            await bus2.start()
            assert await bus2.drain_queue(channel_address("web").agent_id) == [reply]  # the reply is replayed, the status is not

        asyncio.run(_run())

    def test_messages_not_cross_delivered(self, tmp_path: Path) -> None:
        async def _run() -> None:
            bus = MessageBus(tmp_path)
            await bus.start()
            env_mgr = _make_envelope(target=MANAGER_ADDRESS)
            env_planner = _make_envelope(target=planner_address("t1"))
            await bus.publish(env_mgr)
            await bus.publish(env_planner)

            mgr_msgs = await bus.drain_queue("manager")
            planner_msgs = await bus.drain_queue("planner-t1")
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
    @pytest.mark.parametrize("target", [MANAGER_ADDRESS, AgentAddress("manager", "manager-review")])
    def test_messages_queued_before_start_are_not_replayed(self, tmp_path: Path, target: AgentAddress) -> None:
        async def _run() -> None:
            backlog = _make_envelope(source=channel_address("lark"), target=target)
            MessageJournal(tmp_path / ".local" / "agent-bus").append(backlog)
            bus = MessageBus(tmp_path)
            observed: list[MessageEnvelope] = []
            bus.subscribe_topic(MessageType.USER_MESSAGE, observed.append)
            incoming = _make_envelope(source=channel_address("lark"), target=target)
            await bus.publish(incoming)
            assert bus.pending_count(target.agent_id) == 1

            await bus.start()

            assert bus.pending_count(target.agent_id) == 2
            assert bus.take_queue(target.agent_id) == [incoming, backlog]
            assert observed == [incoming, backlog]
            # Skipping a replay must not acknowledge a message the Manager has not handled.
            assert bus.journal.undelivered() == [backlog, incoming]
            await bus.shutdown()
            restarted = MessageBus(tmp_path)
            await restarted.start()
            assert await restarted.drain_queue(target.agent_id) == [backlog, incoming]
            await restarted.shutdown()

        asyncio.run(_run())

    @pytest.mark.parametrize("checkpoint,after", [("journal.jsonl", False), ("journal.jsonl", True),
                                                  ("notice-identities-v1", False), ("notice-identities-v1", True)])
    def test_notice_transition_survives_every_replace_boundary(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, checkpoint: str, after: bool,
    ) -> None:
        from looper.agents.task_notice import Notice, NotifyChannel, NotifyTarget, TargetKind

        journal = MessageJournal(tmp_path / ".local" / "agent-bus")
        notice = Notice(NotifyChannel.TELEGRAM, NotifyTarget(TargetKind.CHAT, "7"), "done",
                        channel_id="channel-telegram")
        current = _make_envelope(source=MANAGER_ADDRESS, target=channel_address("telegram"),
                                 message_type=MessageType.CHAT_NOTICE, payload=notice.payload())
        historical = _make_envelope(source=MANAGER_ADDRESS, target=channel_address("telegram"),
                                    message_type=MessageType.CHAT_NOTICE,
                                    payload={key: value for key, value in notice.payload().items() if key != "channel_id"})
        stale = _make_envelope(message_type=MessageType.STATUS_UPDATE)
        delivered = _make_envelope()
        for envelope in (delivered, current, stale, historical):
            journal.append(envelope)
        journal.mark_delivered(delivered.message_id)
        before = journal._journal_path.read_bytes()
        tracking = journal._delivered_path().read_bytes()
        replace = Path.replace

        def interrupted(path: Path, target: Path) -> Path:
            if target.name == checkpoint:
                if after:
                    replace(path, target)
                raise OSError("interrupted")
            return replace(path, target)

        async def run() -> None:
            bus = MessageBus(tmp_path)
            with monkeypatch.context() as patch:
                patch.setattr(Path, "replace", interrupted)
                with pytest.raises(OSError, match="interrupted"):
                    await bus.start()
            assert journal._delivered_path().read_bytes() == tracking
            if checkpoint == "journal.jsonl" and not after:
                assert journal._journal_path.read_bytes() == before
            # The only transport is a fake consuming subscriber; no channel login or network.
            sent = []
            for _ in range(2):
                restarted = MessageBus(tmp_path)
                restarted.subscribe_topic(MessageType.CHAT_NOTICE, lambda envelope: sent.append(envelope) or True,
                                          consumes=True)
                await restarted.start()
                assert restarted.pending_count(delivered.target.agent_id) == 0
                await restarted.shutdown()
            assert [envelope.message_id for envelope in sent] == [current.message_id, historical.message_id]
            assert sent[0] == current and sent[1].payload == notice.payload()
            assert Notice.from_payload(sent[1].payload) == notice

        asyncio.run(run())

    @pytest.mark.parametrize("change", [{"channel": "unknown"}, {"channel_id": "channel-lark"},
                                         {"channel_id": None}, {"text": ""}])
    def test_invalid_notice_keeps_the_complete_journal(self, tmp_path: Path, change: dict) -> None:
        journal = MessageJournal(tmp_path / ".local" / "agent-bus")
        journal.append(_make_envelope())
        journal.append(_make_envelope(target=channel_address("telegram"), message_type=MessageType.CHAT_NOTICE,
                                     payload={"channel": "telegram", "kind": "chat", "chat": "7", "text": "done", **change}))
        before = journal._journal_path.read_bytes()
        async def run() -> None:
            bus = MessageBus(tmp_path)
            try:
                with pytest.raises(ValueError, match="invalid journal notice.*:2:"):
                    await bus.start()
            finally:
                await bus.shutdown()

        asyncio.run(run())
        assert journal._journal_path.read_bytes() == before
        assert not (journal._dir / "notice-identities-v1").exists()

    def test_messages_for_per_task_agents_are_dropped(self, tmp_path: Path) -> None:
        """Planners and Workers do not survive a restart; their tasks are stopped."""
        journal = MessageJournal(tmp_path / ".local" / "agent-bus")
        for_manager = _make_envelope(target=MANAGER_ADDRESS)
        stale = [
            _make_envelope(target=worker_address("w1"), message_type=MessageType.WORK_ITEM),
            _make_envelope(target=planner_address("t1"), message_type=MessageType.WORK_RESULT),
            _make_envelope(target=planner_address("t2"), message_type=MessageType.WORK_RESULT),
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
        assert bus.pending_count("planner-t2") == 0
        # Only the replayed message remains in the journal; the stale ones are gone.
        lines = (tmp_path / ".local" / "agent-bus" / "journal.jsonl").read_text().splitlines()
        assert [json.loads(line)["message_id"] for line in lines] == [for_manager.message_id]
        assert [e.message_id for e in MessageJournal(tmp_path / ".local" / "agent-bus").undelivered()] == [
            for_manager.message_id
        ]


class TestWorkerPoolConsumesWork:
    def test_dispatched_work_is_delivered_once(self, tmp_path: Path) -> None:
        from unittest.mock import AsyncMock

        from looper.agents.tiers.worker import WorkerConfig, WorkerPool

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
