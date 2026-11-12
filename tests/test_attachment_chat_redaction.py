"""Chat replies never expose attachment storage paths, including streamed fragments."""

import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from looper.agents import chat_attachments as files
from looper.agents.bus import MessageBus
from looper.agents.channels.allowlist import SenderAllowlist
from looper.agents.channels.telegram import TelegramChannelAdapter
from looper.agents.channels.web import WebChannelAdapter
from looper.agents.message import MANAGER_ADDRESS, MessageEnvelope, MessageType, channel_address
from looper.agents.task_notice import NotifyTarget, TargetKind
from looper.agents.tiers.manager import reply_envelope


def _attachment(tmp_path):
    return files.store_upload(tmp_path, "web", "message", [("my notes 中文.txt", b"hello")])[0]


def test_exact_redaction_preserves_following_prose_and_survives_cleanup(tmp_path):
    item = _attachment(tmp_path)
    text = f"Read `{item.path}` then continue."
    assert files.redact(text, tmp_path) == "Read `[attachment]` then continue."
    Path(item.path).unlink()
    assert files.redact(text, tmp_path, paths=[item.path]) == "Read `[attachment]` then continue."


@pytest.mark.parametrize("channel", ["web", "lark", "telegram"])
def test_manager_reply_keeps_attachment_paths_for_recovered_delivery(tmp_path, channel):
    item = _attachment(tmp_path)
    original = MessageEnvelope.create(
        source=channel_address(channel), target=MANAGER_ADDRESS, message_type=MessageType.USER_MESSAGE,
        payload={"text": "read", "attachments": [item.to_payload()],
                 "channel_context": {"session_id": "session", "client_message_id": "message", "chat_id": "7"}})
    answer = reply_envelope(original, item.path, MANAGER_ADDRESS)
    assert answer.metadata["attachment_paths"] == [item.path]


@pytest.mark.parametrize("kind", ["answer", "progress", "phase", "post", "notify"])
def test_telegram_redacts_every_delivery_boundary(tmp_path, kind):
    async def run():
        item = _attachment(tmp_path)
        bus = MessageBus(runtime_root=tmp_path)
        adapter = TelegramChannelAdapter(allowlist=SenderAllowlist("channel-telegram", frozenset({"7"})))
        bot = SimpleNamespace(send_message=AsyncMock(return_value=SimpleNamespace(message_id=1)),
                              edit_message_text=AsyncMock())
        adapter._bus, adapter._app = bus, SimpleNamespace(bot=bot)
        text = f"Read {item.path} please."
        if kind in {"post", "notify"}:
            if kind == "post":
                receipt = await adapter.post(NotifyTarget(TargetKind.CHAT, "7"), text)
            else:
                receipt = await adapter.notify("7", text)
        else:
            Path(item.path).unlink()  # metadata must protect replies after housekeeping removed the file
            payload = {"text": text}
            if kind == "phase":
                payload["phase"] = "working"
            envelope = MessageEnvelope.create(
                source=MANAGER_ADDRESS, target=adapter._address,
                message_type=MessageType.AGENT_REPLY if kind == "answer" else MessageType.STATUS_UPDATE,
                payload=payload, metadata={"reply_route": {"chat_id": "7"}, "attachment_paths": [item.path]})
            receipt = await adapter.deliver(envelope)
        assert receipt.success
        assert bot.send_message.await_count == 1
        sent = bot.send_message.await_args.kwargs["text"]
        assert str(tmp_path) not in sent and "notes 中文.txt" not in sent
        assert "[attachment]" in sent
        if kind == "phase":
            await adapter._status.finish(envelope.correlation_id)
        await bus.shutdown()

    asyncio.run(run())


def test_web_attachment_stream_suppresses_split_paths_and_redacts_final_history(tmp_path, monkeypatch):
    from looper.db import chat

    saved = AsyncMock()
    monkeypatch.setattr(chat, "save_message", saved)

    async def run():
        item = _attachment(tmp_path)
        bus = MessageBus(runtime_root=tmp_path)
        adapter = WebChannelAdapter()
        await adapter.start(bus)
        try:
            received = await adapter.handle_user_message(text="read", session_id="session", message_id="message",
                                                         attachments=[item.to_payload()])
            original = await bus.get_queue("manager").get()
            correlation = received["correlation_id"]
            for fragment in (item.path[:10], item.path[10:]):
                envelope = MessageEnvelope.create(
                    source=MANAGER_ADDRESS, target=adapter._address, message_type=MessageType.STATUS_UPDATE,
                    payload={"text": fragment, "delta": True}, correlation_id=correlation)
                assert (await adapter.deliver(envelope)).success
            assert adapter._sse_queues[correlation].empty()
            Path(item.path).unlink()
            answer = reply_envelope(original, f"Read {item.path} please.", MANAGER_ADDRESS)
            assert (await adapter.deliver(answer)).success
            event = adapter._sse_queues[correlation].get_nowait()
            assert event["payload"]["text"] == "Read [attachment] please."
            assert saved.await_args.kwargs["content"] == "Read [attachment] please."
            assert answer.payload["text"].endswith("notes 中文.txt please.")  # immutable bus envelope
        finally:
            await adapter.stop()
            await bus.shutdown()

    asyncio.run(run())


@pytest.mark.parametrize("channel", ["lark", "telegram"])
def test_channel_history_keeps_the_redacted_answer(tmp_path, monkeypatch, channel):
    from looper.agents.channels.chat_history import CHAT_RECORD, record_reply
    from looper.agents.channels.base import DeliveryReceipt

    async def run():
        item = _attachment(tmp_path)
        envelope = MessageEnvelope.create(source=MANAGER_ADDRESS, target=channel_address(channel),
            message_type=MessageType.AGENT_REPLY, payload={"text": f"Read {item.path} please."},
            metadata={CHAT_RECORD: {"session_id": "session", "message_id": "message"},
                      "attachment_paths": [item.path]})
        Path(item.path).unlink()
        save = AsyncMock()
        monkeypatch.setattr("looper.db.chat.save_channel_message", save)
        await record_reply(channel, envelope, DeliveryReceipt(channel=channel, success=True), runtime_root=tmp_path)
        assert save.await_args.kwargs["content"] == "Read [attachment] please."

    asyncio.run(run())
