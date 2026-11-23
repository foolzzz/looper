"""Session lane ordering, independent completion deadlines, and the fixed off baseline."""
from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from looper.agents import chat_settings, completion_events, lanes_settings, session_routing, turn_metrics
from looper.agents.ops import TurnOrigin
from looper.agents.lanes import Lane
from looper.agents.message import MANAGER_ADDRESS, MessageEnvelope, MessageType, channel_address
from looper.agents.tiers.manager import ManagerAgent, ManagerConfig
from looper.agents.tiers.manager_routing import CompletionState
from looper.db import manager_events

# Observed at ecc2eedbf2ac4a1b44ff9078e634002c228458c1: one FIFO when routed;
# routing off, an empty main worker is not a target and completion runs in the loop.
BASELINE_ORDER = ["user-1", "completion", "user-2"]


def message(text):
    return MessageEnvelope.create(source=channel_address("web"), target=MANAGER_ADDRESS,
        message_type=MessageType.USER_MESSAGE,
        payload={"text": text, "channel_context": {"session_id": "synthetic-chat"}})


def rows(*, age=0):
    return [{"id": 1, "task_id": "synthetic-task", "manager_id": "manager", "target_kind": "web",
             "channel_id": "channel-web", "chat": "synthetic-chat", "payload": {}, "fallback": [],
             "created_at": datetime.now(timezone.utc) - timedelta(seconds=age)}]


def setup(root, monkeypatch, *, enabled=True, routing=False):
    monkeypatch.setattr(lanes_settings, "_enabled", enabled)
    monkeypatch.setattr(session_routing, "_routing_enabled", routing)
    agent = ManagerAgent(config=ManagerConfig(work_dir=root), runtime_root=root)
    agent._running = True
    agent._router._operator_id = AsyncMock(return_value=None)
    agent._bus = SimpleNamespace(settle_handled=lambda e: settled.append(e.message_id))
    settled = []
    return agent, agent._sessions["main"], settled


@pytest.mark.parametrize("routing", [False, True])
def test_off_fixed_target_and_fifo_baseline(monkeypatch, tmp_path, routing):
    async def check():
        agent, session, settled = setup(tmp_path, monkeypatch, enabled=False, routing=routing)
        first, second = message("user-1"), message("user-2")
        assert (await agent._router.target(first) is session) is routing
        batch = uuid.uuid4()
        wake = completion_events.wake(MANAGER_ADDRESS, completion_events.Target.of(rows()[0]))
        assert await agent._router.target(wake) is None
        seen = []
        async def handle(e):
            seen.append(e.payload["text"])
        async def tell(*args):
            seen.append("completion")
        agent._handle, agent._tell_completion = handle, tell
        agent._router.dispatch(session, first)
        agent._router.dispatch_completion(wake, rows(), batch, None)
        agent._router.dispatch(session, second)
        try:
            await asyncio.wait_for(agent._router._workers["main"], 1) if not routing else await wait_settled(settled, 3)
            assert seen == BASELINE_ORDER
            assert settled == [first.message_id, wake.message_id, second.message_id]
        finally:
            await agent._router.stop()
    asyncio.run(check())


async def wait_settled(settled, n):
    async with asyncio.timeout(2):
        while len(settled) < n:
            await asyncio.sleep(0)


def test_user_overtakes_waiting_completion_and_metrics_only_count_turns_that_ran(monkeypatch, tmp_path):
    async def check():
        agent, session, settled = setup(tmp_path, monkeypatch)
        first, second = message("user-1"), message("user-2")
        wake = completion_events.wake(MANAGER_ADDRESS, completion_events.Target.of(rows()[0]))
        seen = []
        monkeypatch.setattr(chat_settings, "_metrics_enabled", True)
        received = datetime.now(timezone.utc)
        async def handle(e):
            seen.append(e.payload["text"])
            if e is first:
                session.lock_owner = object()
                turn_metrics.note_lock_end(session, session.lock_owner, TurnOrigin.USER)
                session.lock_owner = None
            else:
                assert turn_metrics.queued_behind(session, received) is turn_metrics.QueuedBehind.USER
        async def tell(*args):
            seen.append("completion")
        agent._handle, agent._tell_completion = handle, tell
        agent._router.dispatch(session, first)
        agent._router.dispatch_completion(wake, rows(), uuid.uuid4(), None)
        agent._router.dispatch(session, second)
        assert agent._router.queued_count(session, Lane.INTERACTIVE) == 2
        assert agent._router.queued_count(session, Lane.BACKGROUND) == 1
        assert not agent._router.idle(session)
        try:
            await wait_settled(settled, 3)
            assert seen == ["user-1", "user-2", "completion"]
            assert not agent._router.holds(session)
        finally:
            await agent._router.stop()
    asyncio.run(check())


@pytest.mark.parametrize("overdue", [False, True])
def test_deadline_falls_back_without_worker_and_settles_after_write(monkeypatch, tmp_path, overdue):
    async def check():
        agent, session, settled = setup(tmp_path, monkeypatch)
        monkeypatch.setattr(manager_events, "QUEUE_SECONDS", 0.03)
        started, release = asyncio.Event(), asyncio.Event()
        fallback_started, fallback_release = asyncio.Event(), asyncio.Event()
        async def handle(e):
            started.set()
            await release.wait()
        async def fallback(batch, reason, state):
            assert reason == "queue_deadline"
            fallback_started.set()
            await fallback_release.wait()
        agent._handle, agent._fall_back_completion = handle, fallback
        agent._tell_completion = AsyncMock()
        agent._router.dispatch(session, message("hold"))
        await asyncio.wait_for(started.wait(), 2)
        events = rows(age=1 if overdue else 0)
        # Earliest event, not the latest and not the claim time, anchors the deadline.
        events.append({**events[0], "id": 2, "created_at": datetime.now(timezone.utc)})
        wake = completion_events.wake(MANAGER_ADDRESS, completion_events.Target.of(events[0]))
        agent._router.dispatch_completion(wake, events, uuid.uuid4(), None)
        try:
            await asyncio.wait_for(fallback_started.wait(), 1)
            assert wake.message_id not in settled
            assert agent._router.holds_completion(wake)
            assert agent._router.queued_count(session, Lane.BACKGROUND) == 0
            fallback_release.set()
            await wait_settled(settled, 1)
            assert settled == [wake.message_id]
            release.set()
            await wait_settled(settled, 2)
            agent._tell_completion.assert_not_awaited()
        finally:
            release.set()
            await agent._router.stop()
    asyncio.run(check())


@pytest.mark.parametrize("winner", [CompletionState.STARTED, CompletionState.EXPIRED])
def test_deadline_and_worker_have_exactly_one_winner(monkeypatch, tmp_path, winner):
    async def check():
        agent, session, settled = setup(tmp_path, monkeypatch)
        agent._tell_completion = AsyncMock()
        agent._fall_back_completion = AsyncMock()
        wake = completion_events.wake(MANAGER_ADDRESS, completion_events.Target.of(rows()[0]))
        agent._router.dispatch_completion(wake, rows(), uuid.uuid4(), None)
        item = agent._router._queues["main"].background[0][1]
        if winner is CompletionState.EXPIRED:
            agent._router._expire_completion(session, item)
        await wait_settled(settled, 1)
        # Calling the other contender again must do nothing.
        agent._router._expire_completion(session, item)
        await asyncio.sleep(0)
        assert item.state is winner
        assert agent._tell_completion.await_count == (winner is CompletionState.STARTED)
        assert agent._fall_back_completion.await_count == (winner is CompletionState.EXPIRED)
        assert settled == [wake.message_id]
        await agent._router.stop()
    asyncio.run(check())
