"""Channel ids name a bot, not a channel type (``message.parse_channel_id``): task notices and channel posts are
addressed to the id. No database."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest

from looper.agents.bus import MessageBus
from looper.agents.channels.base import ChannelAdapter, DeliveryReceipt, PostReceipt
from looper.agents.channels.chat_routing import RoutingSnapshot
from looper.agents.channels.registry import ChannelRegistry
from looper.agents.message import (
    MANAGER_ADDRESS, TASK_ROUTER_ADDRESS, MessageEnvelope, MessageType, builtin_channel_id, parse_channel_id,
)
from looper.agents.task_notice import Notice, NotifyChannel, NotifyTarget, TargetKind, notify_channel
from looper.agents.tiers.manager import ManagerAgent, ManagerConfig
from looper.agents.tiers.task import Task, TaskRouter

CHAT = NotifyTarget(TargetKind.CHAT, "1000001")


@pytest.mark.parametrize(("channel_id", "channel_type", "number"), [
    ("channel-telegram", "telegram", None), ("channel-lark", "lark", None), ("channel-lark_bot", "lark_bot", None),
    ("channel-telegram-2", "telegram", 2), ("channel-lark-7", "lark", 7), ("channel-lark_bot-3", "lark_bot", 3),
])
def test_a_bot_channel_id_parses_and_round_trips(channel_id: str, channel_type: str, number: int | None) -> None:
    parsed = parse_channel_id(channel_id)
    assert parsed is not None and (parsed.channel_type, parsed.number) == (channel_type, number)
    assert parsed.builtin is (number is None) and str(parsed) == channel_id
    channel = notify_channel(channel_id)
    assert channel is not None and channel.value == channel_type


@pytest.mark.parametrize("channel_id", [
    "channel-web", "", None, "channel-slack", "channel-telegram-0", "channel-telegram-01", "channel-telegram-x",
    "channel-lark-bot-2",
])
def test_anything_else_is_not_a_bot_channel(channel_id: str | None) -> None:
    assert parse_channel_id(channel_id) is None and notify_channel(channel_id) is None


def test_builtin_channel_id() -> None:
    assert builtin_channel_id("lark_bot") == "channel-lark_bot"
    with pytest.raises(ValueError):
        builtin_channel_id("web")


def test_the_notice_carries_its_channel_id() -> None:
    notice = Notice(NotifyChannel.TELEGRAM, CHAT, "t")
    assert notice.channel_id == "channel-telegram" and notice.payload()["channel_id"] == "channel-telegram"
    added = Notice(NotifyChannel.TELEGRAM, CHAT, "t", channel_id="channel-telegram-2")
    assert Notice.from_payload(added.payload()) == added


def test_a_payload_from_before_the_upgrade_is_the_builtin_bot() -> None:
    payload = {"channel": "lark", "chat": "oc_x", "kind": "chat", "mention": None, "text": "t"}
    notice = Notice.from_payload(payload)
    assert notice is not None and notice.channel_id == "channel-lark"


def test_a_notice_of_another_type_is_malformed() -> None:
    payload = {"channel": "lark", "channel_id": "channel-telegram-2", "chat": "oc_x", "kind": "chat", "text": "t"}
    assert Notice.from_payload(payload) is None
    assert Notice.from_payload({**payload, "channel_id": "bogus"}) is None
    with pytest.raises(ValueError):
        Notice(NotifyChannel.LARK, CHAT, "t", channel_id="channel-telegram-2")


class _Stub(ChannelAdapter):
    def __init__(self, channel_id: str, channel_type: str) -> None:
        self._id, self._type = channel_id, channel_type
        self.calls: list[tuple[Any, ...]] = []

    @property
    def channel_type(self) -> str:
        return self._type

    @property
    def channel_id(self) -> str:
        return self._id

    async def start(self, bus: MessageBus) -> None:
        pass

    async def stop(self) -> None:
        pass

    async def deliver(self, envelope: MessageEnvelope) -> DeliveryReceipt:
        return DeliveryReceipt(channel=self._type, success=True)

    async def notify(self, chat: str, text: str) -> DeliveryReceipt:
        self.calls.append(("notify", chat, text))
        return DeliveryReceipt(channel=self._type, success=True)

    async def post(self, target: NotifyTarget, text: str, *, reply_to: str | None = None,
                   idempotency_key: str | None = None) -> PostReceipt:
        self.calls.append(("post", target, text))
        return PostReceipt(True, external_ids=["m1"])


def test_the_registry_addresses_a_bot_by_its_channel_id(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    snapshot = RoutingSnapshot("channel-telegram-2", "manager", False, {})
    monkeypatch.setattr("looper.agents.channels.registry.load_snapshot", AsyncMock(return_value=snapshot))

    async def main() -> None:
        bus = MessageBus(runtime_root=tmp_path)
        registry = ChannelRegistry(bus)
        stub = _Stub("channel-telegram-2", "telegram")
        await registry.register(stub)
        assert await registry.notify("channel-telegram-2", "7", "hi") == (True, None)
        assert stub.calls == [("notify", "7", "hi")]
        assert await registry.notify("channel-telegram", "7", "hi") == (
            False, "the telegram channel is not running: enable it in Settings → Channels")
        receipt = await registry.post("channel-lark-3", CHAT, "hi")
        assert not receipt.success
        assert receipt.error == "the bot channel-lark-3 is not running: enable it in Settings → Channels"
        assert (await registry.post("channel-telegram-2", CHAT, "hi")).success
        await bus.shutdown()

    asyncio.run(main())


@pytest.mark.parametrize(("origin", "channel", "target_type"), [
    ("channel-lark-4", NotifyChannel.LARK, CHAT), ("channel-telegram", NotifyChannel.TELEGRAM, CHAT),
])
def test_the_task_notice_names_the_origin_bot(tmp_path: Path, origin: str, channel: NotifyChannel,
                                              target_type: NotifyTarget) -> None:
    router = TaskRouter(runtime_root=tmp_path, dashboard_url="http://looper.test:8767")
    task = Task(task_id="t1", requirement="r", goal_id="g", work_dir="/w", project_id="p", origin_channel_id=origin,
                notify=target_type, title="x")
    notice = router._notice(task, "completed", "done")
    assert notice is not None and notice.channel is channel and notice.channel_id == origin


def test_the_manager_relays_a_notice_to_the_added_bot(tmp_path: Path) -> None:
    async def main() -> list[Any]:
        bus = MessageBus(runtime_root=tmp_path)
        manager = ManagerAgent(config=ManagerConfig(work_dir=tmp_path), runtime_root=tmp_path)
        manager._bus = bus
        notice = Notice(NotifyChannel.TELEGRAM, CHAT, "✅ x", channel_id="channel-telegram-2")
        await manager._handle(MessageEnvelope.create(
            source=TASK_ROUTER_ADDRESS, target=MANAGER_ADDRESS, message_type=MessageType.GOAL_STATUS,
            payload={"task_id": "t1", "status": "completed", "summary": "s", "notice": notice.payload()},
            correlation_id="c1"))
        queue = bus.get_queue("channel-telegram-2")
        out = [queue.get_nowait() for _ in range(queue.qsize())]
        await bus.shutdown()
        return out

    (envelope,) = asyncio.run(main())
    assert envelope.message_type is MessageType.CHAT_NOTICE and envelope.target.agent_id == "channel-telegram-2"
