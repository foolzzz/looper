"""Only handled user messages and background work advance a session's idle clock."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from looper.agents import chat_settings, completion_events, idle_checks, lanes_settings, mid_turn_input, session_routing
from looper.agents.message import MANAGER_ADDRESS, MessageEnvelope, MessageType, channel_address
from looper.agents.ops import TurnOrigin
from looper.agents.session_routing import MAIN_KEY
from looper.agents.tiers import background_turn, idle_check_turn, manager as manager_module
from looper.agents.tiers.manager import ManagerAgent, ManagerConfig
from tests.db_support import until


def _agent(root, monkeypatch, routing):
    clock = [100.0]
    monkeypatch.setattr(lanes_settings, "_enabled", False)
    monkeypatch.setattr(session_routing, "_routing_enabled", routing)
    monkeypatch.setattr(mid_turn_input, "enabled", lambda _: False)
    agent = ManagerAgent(config=ManagerConfig(work_dir=root), runtime_root=root, clock=lambda: clock[0])
    agent._running = True
    agent._sessions["main"].turn_ended_at = 100.0
    agent._router._user_key = AsyncMock(return_value=MAIN_KEY)
    settled = []
    agent._bus = SimpleNamespace(settle_handled=lambda e: settled.append(e.message_id))
    return agent, clock, settled


async def _loop(agent, envelope, settled):
    async def subscribe(*args, **kwargs):
        yield envelope
    agent._bus.subscribe = subscribe
    try:
        await agent._message_loop()
        await until(lambda: envelope.message_id in settled)
    finally:
        await agent._router.stop()


@pytest.mark.parametrize("routing,expected", [(False, 200.0), (True, 200.0)])
def test_user_message_end_clock_matches_recorded_baseline(monkeypatch, tmp_path, routing, expected):
    # Per-item values recorded on 5fb0c4d70: handling a USER_MESSAGE advances main,
    # including a code command that ran no model turn, with routing off and on.
    async def check():
        agent, clock, settled = _agent(tmp_path, monkeypatch, routing)
        envelope = MessageEnvelope.create(source=channel_address("web"), target=MANAGER_ADDRESS,
            message_type=MessageType.USER_MESSAGE, payload={"text": "/status"})
        agent._handle_user_message = AsyncMock()
        clock[0] = 200.0
        await _loop(agent, envelope, settled)
        agent._handle_user_message.assert_awaited_once_with(envelope)
        assert agent._sessions["main"].turn_ended_at == expected
    asyncio.run(check())


@pytest.mark.parametrize("routing", [False, True])
@pytest.mark.parametrize("kind", ["unclaimed", "other_manager", "completion_off", "settled_idle"])
def test_wake_without_a_turn_preserves_main_clock(monkeypatch, tmp_path, routing, kind):
    async def check():
        agent, clock, settled = _agent(tmp_path, monkeypatch, routing)
        monkeypatch.setattr(chat_settings, "completion_turns", lambda: chat_settings.CompletionTurns.DM_ONLY)
        claim = AsyncMock(return_value=[])
        monkeypatch.setattr(manager_module.db_manager_events, "claim", claim)
        monkeypatch.setattr(agent._router, "completion_operator", AsyncMock(return_value=None))
        fallback = AsyncMock()
        monkeypatch.setattr(completion_events, "fall_back", fallback)
        if kind == "settled_idle":
            envelope = idle_checks.due_envelope(7)
            monkeypatch.setattr(idle_check_turn.store, "get", AsyncMock(return_value={
                "manager_id": "manager", "state": "silent"}))
        else:
            target = completion_events.Target("manager-other" if kind == "other_manager" else "manager",
                                               "channel-web", "synthetic-clock-chat")
            envelope = completion_events.wake(MANAGER_ADDRESS, target)
            if kind == "completion_off":
                monkeypatch.setattr(chat_settings, "completion_turns", lambda: chat_settings.CompletionTurns.OFF)
        clock[0] = 200.0
        await _loop(agent, envelope, settled)
        assert agent._sessions["main"].turn_ended_at == 100.0
        assert agent._sessions["main"].last_activity == 200.0
        assert claim.await_count == (1 if kind == "unclaimed" else 0)
        assert fallback.await_count == (1 if kind == "completion_off" else 0)
    asyncio.run(check())


@pytest.mark.parametrize("routing", [False, True])
def test_claimed_completion_advances_clock_through_loop_or_worker(monkeypatch, tmp_path, routing):
    async def check():
        agent, clock, settled = _agent(tmp_path, monkeypatch, routing)
        monkeypatch.setattr(chat_settings, "completion_turns", lambda: chat_settings.CompletionTurns.DM_ONLY)
        monkeypatch.setattr(agent._router, "completion_operator", AsyncMock(return_value=None))
        target = completion_events.Target("manager", "channel-web", "synthetic-clock-chat")
        row = {"target_kind": "web", "chat": target.chat, "payload": {}, "fallback": [],
               "created_at": datetime.now(timezone.utc)}
        monkeypatch.setattr(manager_module.db_manager_events, "claim", AsyncMock(return_value=[row]))
        calls, replies = [], []
        async def tell(rows, batch):
            calls.append(rows)
            async def work():
                clock[0] = 200.0
                return "Synthetic completion"
            result = await background_turn.run(agent, origin=TurnOrigin.COMPLETION,
                deadline_seconds=1, fallback=AsyncMock(), state={}, work=work())
            replies.append(result.value())
        monkeypatch.setattr(agent, "_tell_completion", tell)
        await _loop(agent, completion_events.wake(MANAGER_ADDRESS, target), settled)
        assert calls == [[row]]
        assert replies == ["Synthetic completion"]
        assert agent._sessions["main"].turn_ended_at == 200.0
    asyncio.run(check())


@pytest.mark.parametrize("origin", [TurnOrigin.COMPLETION, TurnOrigin.IDLE_CHECK, TurnOrigin.TRIAGE])
@pytest.mark.parametrize("ending", ["reply", "failed", "cancelled", "timeout"])
def test_shared_background_entry_writes_end_clock_on_every_exit(monkeypatch, tmp_path, origin, ending):
    async def check():
        agent, clock, _ = _agent(tmp_path, monkeypatch, False)
        entered = asyncio.Event()
        async def work():
            entered.set()
            clock[0] = 200.0
            if ending in {"cancelled", "timeout"}:
                await asyncio.Event().wait()
            if ending == "failed":
                raise ValueError("synthetic turn failure")
            return "Synthetic reply"
        async def late(task):
            clock[0] = 210.0
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        pending = asyncio.create_task(background_turn.run(agent, origin=origin,
            deadline_seconds=0 if ending == "timeout" else 1, fallback=AsyncMock(), state={}, work=work(), late=late))
        await entered.wait()
        if ending == "cancelled":
            pending.cancel()
            with pytest.raises(asyncio.CancelledError):
                await pending
        else:
            result = await pending
            if ending == "failed":
                with pytest.raises(ValueError, match="synthetic turn failure"):
                    result.value()
            elif ending == "reply":
                assert result.value() == "Synthetic reply"
            else:
                assert result.timed_out
        assert agent._sessions["main"].turn_ended_at == (210.0 if ending == "timeout" else 200.0)
    asyncio.run(check())
