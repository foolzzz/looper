"""Idle wake routing, queue ordering and session-local end clocks."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from looper.agents import idle_checks, lanes, lanes_settings, mid_turn_input, session_routing
from looper.agents.message import MANAGER_ADDRESS, MessageEnvelope, MessageType, channel_address
from looper.agents.ops import TurnOrigin
from looper.agents.session_routing import SessionKey, SessionKind
from looper.agents.tiers.manager import ManagerAgent, ManagerConfig
from tests.db_support import until


def _user(text="hello"):
    return MessageEnvelope.create(source=channel_address("web"), target=MANAGER_ADDRESS,
        message_type=MessageType.USER_MESSAGE, payload={"text": text, "channel_context": {"session_id": "idle-chat"}})


def _agent(root, monkeypatch, *, routing=False, enabled=True):
    monkeypatch.setattr(lanes_settings, "_enabled", enabled)
    monkeypatch.setattr(session_routing, "_routing_enabled", routing)
    monkeypatch.setattr(mid_turn_input, "enabled", lambda _: False)
    agent = ManagerAgent(config=ManagerConfig(work_dir=root), runtime_root=root)
    agent._running = True
    settled = []
    agent._bus = SimpleNamespace(settle_handled=lambda e: settled.append(e.message_id))
    return agent, agent._sessions["main"], settled


@pytest.mark.parametrize("routing,enabled,held", [(r, e, h) for r in (False, True)
                                                 for e in (False, True) for h in (False, True)])
def test_idle_wake_targets_main_without_consulting_chat_identity(monkeypatch, tmp_path, routing, enabled, held):
    async def check():
        agent, main, _ = _agent(tmp_path, monkeypatch, routing=routing, enabled=enabled)
        agent._router._user_key = AsyncMock(side_effect=AssertionError("idle wakes have no incoming chat"))
        if held:
            agent._router._handling["main"] = _user()
        assert (await agent._router.target(idle_checks.due_envelope(1)) is main) is (routing or enabled or held)
        agent._router._user_key.assert_not_awaited()
        assert lanes.manager_lane(TurnOrigin.IDLE_CHECK) is lanes.Lane.BACKGROUND
    asyncio.run(check())


@pytest.mark.parametrize("enabled", [False, True])
def test_idle_wake_uses_background_priority_and_preserves_disabled_fifo(monkeypatch, tmp_path, enabled):
    # Existing FIFO target/order baseline: b977f802f230e31069610bbee1bf0e012898b614.
    async def check():
        agent, main, settled = _agent(tmp_path, monkeypatch, enabled=enabled)
        seen = []
        async def handle(envelope):
            seen.append(envelope.message_type)
        agent._handle = handle
        idle, user = idle_checks.due_envelope(1), _user()
        agent._router.dispatch(main, idle)
        agent._router.dispatch(main, user)
        assert agent._router.queued_count(main, lanes.Lane.BACKGROUND) == 1
        assert agent._router.queued_count(main, lanes.Lane.INTERACTIVE) == 1
        try:
            await until(lambda: len(settled) == 2)
            assert seen == ([MessageType.USER_MESSAGE, MessageType.IDLE_CHECK_DUE] if enabled else
                            [MessageType.IDLE_CHECK_DUE, MessageType.USER_MESSAGE])
        finally:
            await agent._router.stop()
    asyncio.run(check())


def test_only_own_worker_end_updates_the_session_idle_clock(monkeypatch, tmp_path):
    async def check():
        agent, main, settled = _agent(tmp_path, monkeypatch, routing=True)
        clock = [100.0]
        monkeypatch.setattr(agent, "_clock", lambda: clock[0])
        group = agent._router.session(SessionKey(SessionKind.GROUP, "channel-lark", "oc_idle_group"))
        entered, release = asyncio.Event(), asyncio.Event()
        async def handle(envelope):
            assert agent._session_now() is group
            entered.set()
            await release.wait()
        agent._handle = handle
        agent._router.dispatch(group, _user())
        try:
            await entered.wait()
            assert main.turn_ended_at is None and group.turn_ended_at is None
            clock[0] = 1900.0
            release.set()
            await until(lambda: len(settled) == 1)
            assert main.turn_ended_at is None and group.turn_ended_at == 1900.0
        finally:
            await agent._router.stop()
    asyncio.run(check())


@pytest.mark.parametrize("blocked", ["none", "lock", "token", "queue", "resident"])
def test_idle_for_uses_start_time_until_own_turn_ends_and_checks_each_main_busy_state(monkeypatch, tmp_path, blocked):
    async def check():
        clock = [100.0]
        agent = ManagerAgent(config=ManagerConfig(work_dir=tmp_path), runtime_root=tmp_path, clock=lambda: clock[0])
        main = agent._sessions["main"]
        clock[0] = 1900.0
        main.last_activity = 1899.0  # an arrival is not an ended turn
        if blocked == "lock":
            await main.turn_lock.acquire()
        elif blocked == "token":
            main.running_token = SimpleNamespace()
        elif blocked == "queue":
            agent._router._handling["main"] = _user()
        elif blocked == "resident":
            main.claude = SimpleNamespace(started=True, idle=False)
        assert agent.idle_for() == (1800.0 if blocked == "none" else None)
        if blocked == "none":
            main.turn_ended_at = 1500.0
            assert agent.idle_for() == 400.0
    asyncio.run(check())


def test_loop_does_not_reset_main_clock_for_completion_held_by_router(monkeypatch, tmp_path):
    async def check():
        agent, main, settled = _agent(tmp_path, monkeypatch)
        main.turn_ended_at = 100.0
        monkeypatch.setattr(agent, "_clock", lambda: 200.0)
        wake = MessageEnvelope.create(source=channel_address("web"), target=MANAGER_ADDRESS,
            message_type=MessageType.COMPLETION_DUE, payload={})
        async def subscribe(*args, **kwargs):
            yield wake
        agent._bus.subscribe = subscribe
        async def claim(envelope):
            agent._router._completion_ids.add(envelope.message_id)
        agent._handle = claim
        await agent._message_loop()
        assert main.turn_ended_at == 100.0
        assert settled == []
    asyncio.run(check())
