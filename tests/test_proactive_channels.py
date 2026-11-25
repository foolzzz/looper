"""Proactive sends retain their typed origin and the adapter's actual receipt."""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import asyncio
import pytest

from looper.agents.bus import MessageBus
from looper.agents.channels.base import DeliveryReceipt, PostReceipt
from looper.agents.channels.allowlist import SenderAllowlist
from looper.agents.channels.registry import ChannelRegistry
from looper.agents.task_notice import NotifyTarget, TargetKind


@pytest.mark.parametrize("method", ["notify", "post", "post_card"])
@pytest.mark.parametrize("metrics", [False, True])
def test_proactive_context_preserves_legacy_callers_and_transport_json(monkeypatch, tmp_path, method, metrics):
    import json
    from looper.agents import chat_settings, turn_metrics

    monkeypatch.setattr(chat_settings, "_metrics_enabled", metrics)

    async def run():
        stored, recorded, wire = [], [], []

        async def capture(ref, text, **kwargs):
            stored.append((ref, text, kwargs))

        async def transport(*args, **kwargs):
            wire.append(json.dumps(kwargs))
            return (DeliveryReceipt("lark", True) if method == "notify" else PostReceipt(True))

        monkeypatch.setattr("looper.agents.channels.registry.store_body", capture)
        monkeypatch.setattr(turn_metrics, "record_proactive", lambda ref, **kw: recorded.append(ref))
        registry = ChannelRegistry(MessageBus(tmp_path))
        registry._channels["channel-lark"] = SimpleNamespace(**{method: transport})
        target = "synthetic-chat" if method == "notify" else NotifyTarget(TargetKind.CHAT, "synthetic-chat")
        body = SimpleNamespace(text="message") if method == "post_card" else "message"

        # A pre-existing wrapper accepts only transport arguments, with no observation keyword.
        async def legacy_sender(channel, target, body):
            return await getattr(registry, method)(channel, target, body)

        with turn_metrics.proactive_send(turn_metrics.ProactiveSource.SCHEDULE, "synthetic-run", group=True):
            await legacy_sender("channel-lark", target, body)
        await legacy_sender("channel-lark", target, body)
        assert stored[0][0] == turn_metrics.ProactiveRef(turn_metrics.ProactiveSource.SCHEDULE, "synthetic-run")
        assert stored[1][0] is None
        assert stored[0][2]["group"] is (method == "notify")
        assert recorded == [stored[0][0]]
        assert len(wire) == 2 and all("proactive" not in value for value in wire)

    asyncio.run(run())


def test_proactive_context_is_nested_task_local_and_cleared_after_cancellation():
    from looper.agents import turn_metrics

    async def run():
        entered = asyncio.Event()
        observed = []

        async def sending():
            try:
                with turn_metrics.proactive_send(turn_metrics.ProactiveSource.INTENT, "child", group=True):
                    observed.append(turn_metrics.current_proactive())
                    entered.set()
                    await asyncio.Event().wait()
            finally:
                observed.append(turn_metrics.current_proactive())

        with turn_metrics.proactive_send(turn_metrics.ProactiveSource.SCHEDULE, "parent"):
            parent = turn_metrics.current_proactive()
            with turn_metrics.proactive_send(turn_metrics.ProactiveSource.GATE, "nested"):
                assert turn_metrics.current_proactive()[0].source_ref == "nested"
            task = asyncio.create_task(sending())
            await entered.wait()
            assert turn_metrics.current_proactive() == parent
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert turn_metrics.current_proactive() == parent
            assert observed == [(turn_metrics.ProactiveRef(turn_metrics.ProactiveSource.INTENT, "child"), True), parent]
        assert turn_metrics.current_proactive() == (None, False)

    asyncio.run(run())


@pytest.mark.parametrize("method", ["post", "post_card"])
def test_registry_records_post_before_returning_its_receipt(monkeypatch, tmp_path, method):
    async def run():
        from looper.agents import turn_metrics

        calls = []
        monkeypatch.setattr(turn_metrics, "record_proactive", lambda ref, **kw: calls.append((ref, kw)))
        registry = ChannelRegistry(MessageBus(tmp_path))
        receipt = PostReceipt(True, external_ids=("message-a",))
        registry._channels["channel-lark"] = SimpleNamespace(**{method: AsyncMock(return_value=receipt)})
        ref = turn_metrics.ProactiveRef(turn_metrics.ProactiveSource.GATE, "gate-a", "manager")
        target = NotifyTarget(TargetKind.CHAT, "oc_synthetic", group=True)
        body = "question" if method == "post" else SimpleNamespace(text="question")
        assert await getattr(registry, method)("channel-lark", target, body, proactive=ref) is receipt
        record_key = calls[0][1]["dedupe_key"]
        assert isinstance(record_key, str) and record_key
        assert calls == [(ref, {"channel_id": "channel-lark", "chat": "oc_synthetic", "group": True,
                                "external_id": "message-a", "outcome": "sent", "dedupe_key": record_key})]
        await getattr(registry, method)("channel-lark", target, body)
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


@pytest.mark.parametrize("method", ["notify", "post", "post_card"])
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
            body = SimpleNamespace(text="message") if method == "post_card" else "message"
            await getattr(registry, method)("channel-lark", target, body, proactive=ref)
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


@pytest.mark.parametrize("kind", ["direct", "group", "notice", "progress"])
@pytest.mark.parametrize("proactive", [False, True])
def test_lark_metrics_only_request_ids_for_proactive_envelopes(monkeypatch, tmp_path, kind, proactive):
    from looper.agents import chat_settings
    from looper.agents.channels.lark import LarkChannelAdapter
    from looper.agents.message import MANAGER_ADDRESS, MessageEnvelope, MessageType, channel_address
    from looper.agents.task_notice import Notice, NotifyChannel

    monkeypatch.setattr(chat_settings, "_metrics_enabled", True)

    async def run():
        adapter = LarkChannelAdapter(runtime_root=tmp_path, allowlist=SenderAllowlist("channel-lark", frozenset()))
        async def send(*args, **kwargs):
            return "om_sent" if kwargs.get("return_id") else None

        transport = SimpleNamespace(send=AsyncMock(side_effect=send), reply=AsyncMock(side_effect=send))
        adapter._transport.inner = transport
        adapter._status.finish = AsyncMock()
        payload = {"text": "Synthetic answer"}
        message_type = MessageType.AGENT_REPLY
        if kind == "notice":
            payload = Notice(NotifyChannel.LARK, NotifyTarget(TargetKind.CHAT, "oc_synthetic"),
                             "Synthetic notice", "channel-lark").payload()
            message_type = MessageType.CHAT_NOTICE
        elif kind == "progress":
            payload["kind"] = "progress"
            message_type = MessageType.STATUS_UPDATE
        envelope = MessageEnvelope.create(source=MANAGER_ADDRESS, target=channel_address("lark"),
                                         message_type=message_type, payload=payload,
                                         metadata={"proactive": {"source": "task_notice"}} if proactive else {})
        adapter._correlation_chat[envelope.correlation_id] = "oc_synthetic"
        if kind == "group":
            adapter._group_message[envelope.correlation_id] = "om_parent"
        receipt = await adapter.deliver(envelope)
        assert receipt.success
        calls = transport.send.await_args_list + transport.reply.await_args_list
        assert len(calls) == 1
        assert calls[0].kwargs.get("return_id", False) is proactive
        assert receipt.external_id == ("om_sent" if proactive else None)

    asyncio.run(run())


@pytest.mark.parametrize("output, expected", [(b"unreadable output", "sent"), (b"[]", "sent"),
                                              (b'{"ok":false,"error":{"code":230001}}', "rejected")])
def test_lark_proactive_send_retains_success_when_only_receipt_parsing_fails(monkeypatch, tmp_path, output, expected):
    from looper.agents import chat_settings
    from looper.agents.channels import lark_cli
    from looper.agents.channels.lark import LarkChannelAdapter
    from looper.agents.message import MANAGER_ADDRESS, MessageEnvelope, MessageType, channel_address

    monkeypatch.setattr(chat_settings, "_metrics_enabled", True)
    call = AsyncMock(return_value=output)
    monkeypatch.setattr(lark_cli, "run", call)

    async def run():
        adapter = LarkChannelAdapter(runtime_root=tmp_path, allowlist=SenderAllowlist("channel-lark", frozenset()))
        adapter._status.finish = AsyncMock()
        envelope = MessageEnvelope.create(source=MANAGER_ADDRESS, target=channel_address("lark"),
                                         message_type=MessageType.AGENT_REPLY, payload={"text": "Synthetic notice"},
                                         metadata={"proactive": {"source": "task_notice"}})
        adapter._correlation_chat[envelope.correlation_id] = "oc_synthetic"
        receipt = await adapter.deliver(envelope)
        assert receipt.result.value == expected
        assert receipt.external_id is None
        call.assert_awaited_once()

    asyncio.run(run())
