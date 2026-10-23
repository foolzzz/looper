"""A message that came through loopercli carries its API client into the Manager's ops turn."""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path
from typing import Any

import pytest

from looper.agents.bus import MessageBus
from looper.agents.message import MANAGER_ADDRESS, MessageEnvelope, MessageType, channel_address
from looper.agents.ops import Turn
from looper.agents.tiers.manager import ManagerAgent, ManagerConfig, _message_client
from tests.test_manager_reply import _fake_claude, _scripted

WEB = channel_address("web")


def _envelope(context: Any) -> MessageEnvelope:
    return MessageEnvelope.create(
        source=WEB, target=MANAGER_ADDRESS, message_type=MessageType.USER_MESSAGE,
        payload={"text": "hi", "channel_context": context}, correlation_id="c1")


@pytest.mark.parametrize("context, expected", [
    ({"session_id": "s", "client": {"id": "client-1", "name": "laptop"}}, ("client-1", "laptop")),
    ({"session_id": "s", "client": {"id": "", "name": "laptop"}}, (None, None)),
    ({"session_id": "s", "client": {"id": "client-1", "name": ""}}, (None, None)),
    ({"session_id": "s", "client": {"id": "client-1"}}, (None, None)),
    ({"session_id": "s", "client": {"id": 1, "name": "laptop"}}, (None, None)),
    ({"session_id": "s", "client": "laptop"}, (None, None)),
    ({"session_id": "s"}, (None, None)),
    ("not a dict", (None, None)),
    (None, (None, None)),
])
def test_message_client_reads_only_a_well_formed_client(context: Any, expected: tuple[Any, Any]) -> None:
    assert _message_client(_envelope(context)) == expected


@pytest.mark.usefixtures("agent_db")
def test_the_ops_turn_of_a_client_message_carries_the_client() -> None:
    async def _run() -> list[Turn]:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bus = MessageBus(runtime_root=root)
            manager = ManagerAgent(
                config=ManagerConfig(claude_bin=_fake_claude(root, _scripted("ok", "ok")), work_dir=root),
                runtime_root=root)
            manager._bus = bus
            seen: list[Turn] = []
            link = manager._link_turn_task

            async def spy(recorder: Any, ops_turn: Turn | None) -> None:
                if ops_turn is not None:
                    seen.append(ops_turn)
                await link(recorder, ops_turn)

            manager._link_turn_task = spy  # type: ignore[method-assign]
            for index, context in enumerate([
                    {"session_id": "cli-1", "client": {"id": "client-1", "name": "laptop"}},
                    {"session_id": "chat-1"}]):
                await manager._handle(MessageEnvelope.create(
                    source=WEB, target=MANAGER_ADDRESS, message_type=MessageType.USER_MESSAGE,
                    payload={"text": f"message {index}", "channel_context": context}, correlation_id=f"corr-{index}"))
            await manager.stop()
            await bus.shutdown()
            return seen

    client_turn, dashboard_turn = asyncio.run(_run())
    assert (client_turn.client_id, client_turn.client_name) == ("client-1", "laptop")
    assert (dashboard_turn.client_id, dashboard_turn.client_name) == (None, None)
