"""Rich messages traverse the local bot platform, stream, adapter and Manager bus."""

from __future__ import annotations

import asyncio
import json
from contextlib import aclosing
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from lark_platform_fake import FakeLarkPlatform
from looper.agents.bus import MessageBus
from looper.agents.channels.allowlist import SenderAllowlist
from looper.agents.channels import lark
from looper.agents.channels.chat_routing import RouteAction, RouteDecision
from looper.agents.channels.lark_bot import make_adapter
from looper.agents.message import MessageType, channel_address


@pytest.mark.parametrize("admitted", [False, True])
def test_bot_stream_keeps_admission_when_chat_senders_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, admitted: bool,
) -> None:
    monkeypatch.setattr(lark, "observe_chat", AsyncMock())

    async def main() -> None:
        async with FakeLarkPlatform() as fake:
            sender = "ou_sender"
            senders = frozenset({sender}) if admitted else frozenset()
            rejected = []

            async def on_rejected(sender_id: str, chat_id: str | None) -> None:
                rejected.append((sender_id, chat_id))

            bus = MessageBus(runtime_root=tmp_path)
            adapter = make_adapter({"app_id": fake.app_id}, "test-secret", runtime_root=tmp_path,
                allowlist=SenderAllowlist("channel-lark_bot", frozenset({"ou_other"})), on_rejected=on_rejected,
                bot_open_id=fake.bot["open_id"], base_url=fake.base_url,
                resolve_chat=lambda *_: RouteDecision(RouteAction.ADMIT, "manager", senders=senders))
            adapter._bus = bus
            try:
                async with aclosing(adapter._transport.events()) as events:
                    pending = asyncio.create_task(anext(events))
                    await fake.push_message(text="hello", sender=sender)
                    event = await asyncio.wait_for(pending, 2)
                    assert event["content"] == ("hello" if admitted else "")
                    senders = frozenset() if admitted else frozenset({sender})
                    await adapter._handle_lark_event(event)
                    queue = bus.get_queue("manager")
                    if admitted:
                        assert rejected == []
                        assert queue.get_nowait().payload["text"] == "hello"
                        await asyncio.wait_for(asyncio.gather(*adapter._reactions._pending), 2)
                        assert [sent["kind"] for sent in fake.sent] == ["reaction"]
                    else:
                        assert rejected == [(sender, None)]
                        assert queue.empty()
                        assert fake.sent == []
            finally:
                await adapter.stop()
                await bus.shutdown()

    asyncio.run(asyncio.wait_for(main(), 10))


@pytest.mark.parametrize("group", [False, True])
def test_post_from_bot_stream_reaches_manager_with_resource_placeholders(tmp_path: Path, group: bool) -> None:
    async def main() -> None:
        async with FakeLarkPlatform() as fake:
            fake.resources = {("om_rich", "img_v3_a1"): (b"\x89PNG\r\n\x1a\n", "image/png"),
                              ("om_rich", "file_v3_b2"): (b"%PDF-1.7", "application/pdf")}
            sender = "ou_sender"
            fake.users = [{"open_id": sender, "name": "Ada Lovelace"}]
            bot = fake.bot["open_id"]
            mention = {"id": {"open_id": bot}, "key": "@_user_1", "name": "HelperBot"}
            paragraph = [{"tag": "text", "text": "Hello", "style": ["bold"]}, {"tag": "text", "text": " "},
                         {"tag": "a", "text": "Spec", "href": "https://example.com/spec"}]
            if group:
                paragraph = [{"tag": "at", "user_id": "@_user_1", "user_name": ""},
                             {"tag": "text", "text": " "}, *paragraph]
            post = {"title": "Weekly", "content": [paragraph, [{"tag": "img", "image_key": "img_v3_a1"}],
                    [{"tag": "code_block", "language": "go", "text": "    fmt.Println(1)"}]],
                    "files": [{"file_key": "file_v3_b2", "file_name": 'a "q".pdf'}]}
            bus = MessageBus(runtime_root=tmp_path)
            adapter = make_adapter({"app_id": fake.app_id}, "test-secret", runtime_root=tmp_path,
                allowlist=SenderAllowlist("channel-lark_bot", frozenset({sender})), on_rejected=None,
                bot_open_id=bot, base_url=fake.base_url)
            try:
                await adapter.start(bus)
                await fake.push_message(message_type="post", content=json.dumps(post), sender=sender,
                                        chat_type="group" if group else "p2p", message_id="om_rich",
                                        mentions=[mention] if group else [])
                await fake.wait_acks(1)
                assert fake.acks == [("om_rich", {"code": 200})]
                message = await asyncio.wait_for(bus.get_queue("manager").get(), 2)
                assert message.message_type is MessageType.USER_MESSAGE
                assert message.source == channel_address("lark_bot")
                assert message.payload["text"] == (
                    "Weekly\n**Hello** [Spec](https://example.com/spec)\n[image 1]\n\n"
                    '```go\n    fmt.Println(1)\n```\n[file 2: a "q".pdf]')
                attachments = message.payload["attachments"]
                assert [(item["state"], item["mime"]) for item in attachments] == [
                    ("saved", "image/png"), ("saved", "application/pdf")]
                for item in attachments:
                    path = Path(item["path"])
                    assert path.parent == tmp_path / "chat-attachments" / "channel-lark_bot" / "om_rich"
                    assert path.is_file() and path.stat().st_mode & 0o777 == 0o600
                    assert path.parent.stat().st_mode & 0o777 == 0o700
                requests = [request for request in fake.requests if "/resources/" in request["path"]]
                assert [request["query"] for request in requests] == [{"type": ["image"]}, {"type": ["file"]}]
                assert all(request["authenticated"] for request in requests)
                context = message.payload["channel_context"]
                assert context["message_id"] == "om_rich"
                assert context["chat_type"] == ("group" if group else "p2p")
                if group:
                    assert context["sender_name"] == "Ada Lovelace"
                await asyncio.wait_for(asyncio.gather(*adapter._reactions._pending), 2)
                assert [(sent["kind"], sent["parent"]) for sent in fake.sent] == [("reaction", "om_rich")]
            finally:
                await adapter.stop()
                await bus.shutdown()

    asyncio.run(asyncio.wait_for(main(), 10))
