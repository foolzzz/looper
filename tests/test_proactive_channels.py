"""Proactive sends retain their typed origin and the adapter's actual receipt."""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import asyncio
import pytest

from looper.agents.bus import MessageBus
from looper.agents.channels.base import DeliveryReceipt, PostReceipt
from looper.agents.channels.registry import ChannelRegistry
from looper.agents.task_notice import NotifyTarget, TargetKind


def test_registry_records_post_before_returning_its_receipt(monkeypatch, tmp_path):
    async def run():
        from looper.agents import turn_metrics

        calls = []
        monkeypatch.setattr(turn_metrics, "record_proactive", lambda ref, **kw: calls.append((ref, kw)))
        registry = ChannelRegistry(MessageBus(tmp_path))
        receipt = PostReceipt(True, external_ids=("message-a",))
        registry._channels["channel-lark"] = SimpleNamespace(post=AsyncMock(return_value=receipt))
        ref = turn_metrics.ProactiveRef(turn_metrics.ProactiveSource.GATE, "gate-a", "manager")
        target = NotifyTarget(TargetKind.CHAT, "oc_synthetic", group=True)
        assert await registry.post("channel-lark", target, "question", proactive=ref) is receipt
        assert calls == [(ref, {"channel_id": "channel-lark", "chat": "oc_synthetic", "group": True,
                                "external_id": "message-a", "outcome": "sent"})]
        await registry.post("channel-lark", target, "ordinary")
        assert len(calls) == 1


    asyncio.run(run())

def test_notify_preserves_unknown_outcome(monkeypatch, tmp_path):
    async def run():
        from looper.agents import turn_metrics

        calls = []
        monkeypatch.setattr(turn_metrics, "record_proactive", lambda ref, **kw: calls.append(kw))
        registry = ChannelRegistry(MessageBus(tmp_path))
        registry._channels["channel-telegram"] = SimpleNamespace(
            notify=AsyncMock(return_value=DeliveryReceipt("telegram", False, error="lost response")))
        ref = turn_metrics.ProactiveRef(turn_metrics.ProactiveSource.SCHEDULE, "run-a", None)
        assert await registry.notify("channel-telegram", "-42", "notice", proactive=ref, group=True) == (False, "lost response")
        assert calls[0]["outcome"] == "unknown"
        assert calls[0]["group"] is True
    asyncio.run(run())


@pytest.mark.parametrize("method", ["notify", "post"])
def test_a_channel_exception_records_unknown_and_keeps_the_existing_error(monkeypatch, tmp_path, method):
    async def run():
        from looper.agents import turn_metrics

        calls = []
        monkeypatch.setattr(turn_metrics, "record_proactive", lambda ref, **kw: calls.append(kw))
        registry = ChannelRegistry(MessageBus(tmp_path))
        registry._channels["channel-lark"] = SimpleNamespace(**{method: AsyncMock(side_effect=RuntimeError("lost"))})
        ref = turn_metrics.ProactiveRef(turn_metrics.ProactiveSource.SCHEDULE, "run-a")
        target = "oc_synthetic" if method == "notify" else NotifyTarget(TargetKind.CHAT, "oc_synthetic")
        with pytest.raises(RuntimeError, match="lost"):
            await getattr(registry, method)("channel-lark", target, "message", proactive=ref)
        assert len(calls) == 1 and calls[0]["outcome"] == "unknown"

    asyncio.run(run())


@pytest.mark.parametrize("channel", ["telegram", "lark"])
def test_off_outbound_keeps_the_original_delivery(monkeypatch, tmp_path, channel):
    from looper.agents import chat_settings
    from looper.agents.channels import telegram, lark
    from looper.agents.channels.allowlist import SenderAllowlist
    from looper.agents.message import MANAGER_ADDRESS, MessageEnvelope, MessageType, channel_address

    monkeypatch.setattr(chat_settings, "_metrics_enabled", False)

    async def run():
        envelope = MessageEnvelope.create(source=MANAGER_ADDRESS, target=channel_address(channel),
                                         message_type=MessageType.AGENT_REPLY, payload={"text": "A reply"},
                                         metadata={"reply_route": {"chat_id": "synthetic-chat"} if channel == "telegram" else ["legacy"]})

        async def subscribe(channel):
            yield envelope

        module = telegram if channel == "telegram" else lark
        adapter = (telegram.TelegramChannelAdapter(allowlist=SenderAllowlist("channel-telegram", frozenset({"42"})))
                   if channel == "telegram" else lark.LarkChannelAdapter(runtime_root=tmp_path, allowlist=SenderAllowlist("channel-lark", frozenset({"synthetic-sender"}))))
        adapter._running = True
        adapter._bus = SimpleNamespace(subscribe=subscribe, runtime_root=tmp_path)
        delivery = AsyncMock(return_value=DeliveryReceipt(channel, True))
        recorded = AsyncMock()
        metrics = []
        monkeypatch.setattr(adapter, "deliver", delivery)
        monkeypatch.setattr(module, "record_reply", recorded)
        monkeypatch.setattr(module, "record_delivery", lambda *a, **kw: metrics.append(kw))
        await adapter._outbound_loop()
        delivery.assert_awaited_once_with(envelope)
        recorded.assert_awaited_once()
        assert metrics == []

    asyncio.run(run())
