"""Exact DM grammar and the fixed disabled routing baseline."""

import asyncio
from unittest.mock import AsyncMock, Mock

import pytest

from looper.agents import memory_routing
from looper.agents.message import AgentAddress, MessageEnvelope, MessageType, manager_address


def envelope(text, *, channel="lark", sender="ou_operator_synthetic", group=False, quote=None):
    context = {"chat_id": "oc_group_synthetic" if group else "oc_dm_synthetic",
               "sender_id": sender, "chat_type": "group" if group else "p2p", "message_id": "om_reply_synthetic"}
    if channel == "telegram":
        context["chat_id"] = "-70001" if group else "70001"
    if quote:
        context["reply_to_message_id" if channel == "telegram" else "reply_to"] = quote
    return MessageEnvelope.create(source=AgentAddress("channel", "channel-" + channel, channel),
        target=manager_address("manager"), message_type=MessageType.USER_MESSAGE,
        payload={"text": text, "channel_context": context})


@pytest.mark.parametrize("raw", [None, {}, {"enabled": False, "push_channel": "channel-lark"},
                                  {"enabled": True, "push_channel": None}])
@pytest.mark.parametrize("text,quote", [("记 M-1", None), ("驳回 M-1 x", None), ("记", "om_push_synthetic")])
def test_disabled_route_matches_recorded_parent_baseline(monkeypatch, raw, text, quote):
    # Parent 94bfd7e7c: no memory reply router, no memory-post reads or actions for these messages.
    baseline = {"claimed": False, "candidate_reads": 0, "actions": 0}
    monkeypatch.setattr(memory_routing.memory_settings, "stored", AsyncMock(return_value=raw))
    pool = Mock(side_effect=AssertionError("off must not acquire candidate/channel storage"))
    monkeypatch.setattr(memory_routing, "get_pool", pool)
    act = AsyncMock()
    monkeypatch.setattr(memory_routing.memory_actions, "act", act)
    result = asyncio.run(memory_routing.route(envelope(text, quote=quote)))
    assert {"claimed": result is not None, "candidate_reads": pool.call_count,
            "actions": act.await_count} == baseline


@pytest.mark.parametrize("text", ["记", "请记 M-1", "我说记 M-1", "建 I-1", "驳回 I-1", "确认", "确认 L-1"])
def test_unquoted_non_memory_commands_do_not_read_posts(monkeypatch, text):
    monkeypatch.setattr(memory_routing.memory_settings, "stored", AsyncMock(return_value={
        "enabled": True, "push_channel": "channel-lark"}))
    monkeypatch.setattr(memory_routing, "get_pool", Mock(side_effect=AssertionError("no memory reference")))
    assert asyncio.run(memory_routing.route(envelope(text))) is None


@pytest.mark.parametrize("text,quote", [("记 M-1", None), ("驳回 M-1 x", None), ("记", "om_push_synthetic")])
def test_disabled_manager_matches_parent_model_path(monkeypatch, tmp_path, agent_db, text, quote):
    from looper.agents.gate_routing import NOT_A_REPLY
    from tests.test_manager_stop import _Session, _manager, _with_ops

    # Recorded parent 94bfd7e7c983084fe1d8135aa8323cbd09cc2fc6 routes these through intents, then the model.
    baseline = {"model_turns": 1, "intent_checks": 1, "persisted_users": 1}
    async def check():
        monkeypatch.setattr(memory_routing.memory_settings, "stored", AsyncMock(return_value={"enabled": False}))
        session = _Session(hold=False)
        manager, bus, _ = _manager(tmp_path, session)
        _with_ops(manager)
        manager._route_to_gate = AsyncMock(return_value=NOT_A_REPLY)
        manager._route_intent = AsyncMock(return_value=None)
        manager._persist_user = AsyncMock()
        message = envelope(text, quote=quote)
        await manager._handle(message)
        assert {"model_turns": len(session.turns), "intent_checks": manager._route_intent.await_count,
                "persisted_users": manager._persist_user.await_count} == baseline
        assert text in session.turns[0]
        await bus.shutdown()
    asyncio.run(check())
