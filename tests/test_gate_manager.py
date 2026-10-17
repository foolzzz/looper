"""The Manager and user gates: a reply to a gate's message goes to the gate before the model (``gate_routing``),
a reply the gate kept or refused is answered by code and not kept in the chat's history (its answer names no record
for the channel to store), and a reply to a gate that no longer waits reaches the model with a note. The routing itself is checked on a real database in
``test_gate_channels_db``; here it is stubbed.
"""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from looper.agents.bus import MessageBus
from looper.agents.channels.chat_history import CHAT_RECORD
from looper.agents.gate_routing import Route, RouteKind
from looper.agents.message import MANAGER_ADDRESS, MessageEnvelope, MessageType, channel_address
from looper.agents.tiers import manager as manager_module
from looper.agents.tiers.manager import ManagerAgent, ManagerConfig

LARK = channel_address("lark", "channel-lark")


def _handle(monkeypatch: pytest.MonkeyPatch, routed: Route) -> dict[str, Any]:
    """One Lark message to a Manager whose routing answers ``routed``: what it published (with whether the channel
    is to store it), what it stored, and what reached its model."""
    seen: dict[str, Any] = {"model": [], "stored": []}

    async def route(envelope: MessageEnvelope, **_: Any) -> Route:
        return routed

    async def invoke(self: Any, message: str, envelope: MessageEnvelope, *, turn: Any, note: str = "") -> str:
        seen["model"].append((message, note))
        return "the Manager's answer"

    async def persist(self: Any, envelope: MessageEnvelope, role: str, content: str, message_id: str) -> None:
        seen["stored"].append((role, content))

    monkeypatch.setattr(manager_module, "route_to_gate", route)
    monkeypatch.setattr(ManagerAgent, "_invoke_claude", invoke)
    monkeypatch.setattr(ManagerAgent, "_persist_chat", persist)

    async def run() -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bus = MessageBus(runtime_root=root)
            manager = ManagerAgent(config=ManagerConfig(claude_bin="claude", work_dir=root), runtime_root=root)
            manager._bus = bus

            async def tools_problem() -> None:
                return None

            manager.ops = SimpleNamespace(platform=SimpleNamespace(task_router=SimpleNamespace(
                has_planner=lambda task_id: True)), tools_problem=tools_problem)
            await manager._handle(MessageEnvelope.create(
                source=LARK, target=MANAGER_ADDRESS, message_type=MessageType.USER_MESSAGE,
                payload={"text": "PostgreSQL", "channel_context": {"chat_id": "oc_dm", "chat_type": "p2p",
                                                                   "message_id": "om_9", "reply_to": "om_1"}},
                correlation_id="corr-1"))
            queue = bus.get_queue(LARK.agent_id)
            seen["sent"] = []
            while not queue.empty():
                envelope = queue.get_nowait()
                seen["sent"].append((envelope.message_type, envelope.payload.get("text"),
                                     CHAT_RECORD in envelope.metadata))
            await bus.shutdown()

    asyncio.run(run())
    return seen


@pytest.mark.usefixtures("agent_db")
class TestGateReplies:
    def test_a_reply_the_gate_took_never_reaches_the_model(self, monkeypatch: pytest.MonkeyPatch) -> None:
        seen = _handle(monkeypatch, Route(RouteKind.DELIVERED))
        assert seen["model"] == [] and seen["sent"] == [] and seen["stored"] == []

    @pytest.mark.parametrize("kind", [RouteKind.KEPT, RouteKind.REFUSED])
    def test_a_reply_the_gate_kept_or_refused_is_answered_by_code_and_not_stored(
        self, monkeypatch: pytest.MonkeyPatch, kind: RouteKind,
    ) -> None:
        seen = _handle(monkeypatch, Route(kind, reply="任务已停止，你的回答已保存，任务继续后交给规划者。"))
        assert seen["model"] == [] and seen["stored"] == []
        assert seen["sent"] == [(MessageType.AGENT_REPLY, "任务已停止，你的回答已保存，任务继续后交给规划者。", False)]

    def test_a_reply_to_a_closed_gate_reaches_the_model_with_a_note(self, monkeypatch: pytest.MonkeyPatch) -> None:
        note = "[Looper] This message replies to a message of gate g1 of task task-a, which no longer takes answers"
        seen = _handle(monkeypatch, Route(RouteKind.CLOSED, note=note))
        assert seen["model"] == [("PostgreSQL", note)]
        assert seen["stored"] == [("user", "PostgreSQL")]
        assert (MessageType.AGENT_REPLY, "the Manager's answer", True) in seen["sent"]

    def test_any_other_message_is_the_managers(self, monkeypatch: pytest.MonkeyPatch) -> None:
        seen = _handle(monkeypatch, Route(RouteKind.NONE))
        assert seen["model"] == [("PostgreSQL", "")]
        assert (MessageType.AGENT_REPLY, "the Manager's answer", True) in seen["sent"]
