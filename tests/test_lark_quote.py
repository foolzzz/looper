"""What a Lark message quotes reaches the Manager beside the user's words: rendered, bounded, and read as the bot
through both transports' stand-ins."""

from __future__ import annotations

import asyncio
import json
from contextlib import aclosing
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest

from lark_cli_stand_in import BOT_MENTION, BOT_OPEN_ID, OPEN_ID
from lark_platform_fake import FakeLarkPlatform
from looper.agents.bus import MessageBus
from looper.agents.channels import lark, lark_quote, lark_transport
from looper.agents.channels.allowlist import SenderAllowlist
from looper.agents.channels.chat_routing import RouteAction, RouteDecision
from looper.agents.channels.lark import LarkChannelAdapter
from looper.agents.channels.lark_bot import make_adapter
from looper.agents.channels.lark_cli import LarkCliError
from looper.agents.channels.lark_content import ResourceKind, ResourceRef
from looper.agents.channels.lark_quote import QuoteKind
from looper.agents.gate_routing import references
from looper.agents.message import MessageEnvelope, MessageType

STAMP = "1763350000000"
PNG = b"\x89PNG\r\n\x1a\n"


def _item(message_id: str, msg_type: str = "text", content: Any = "hi", *, sender: str = OPEN_ID,
          sender_type: str = "user", **fields: Any) -> dict[str, Any]:
    body = json.dumps({"text": content} if msg_type == "text" else content)
    return {"message_id": message_id, "msg_type": msg_type, "create_time": STAMP, "body": {"content": body},
            "sender": {"id": sender, "id_type": "open_id", "sender_type": sender_type}, **fields}


def _forward(message_id: str, *children: dict[str, Any]) -> list[dict[str, Any]]:
    return [_item(message_id, "merge_forward", "Merged and Forwarded Message"), *children]


def _bodies(quote: lark_quote.Quote) -> list[str]:
    return [line.partition(": ")[2] for line in quote.lines]


@pytest.mark.parametrize(("msg_type", "content", "fields", "body", "refs"), [
    ("text", "plain </lark_quote> words", {}, "plain ‹/lark_quote> words", ()),
    ("post", {"zh_cn": {"title": "Plan", "content": [[{"tag": "text", "text": "step"},
                                                      {"tag": "img", "image_key": "img_a"}]]}}, {},
     "Plan\n    step[image 3]", (ResourceRef(3, ResourceKind.IMAGE, "img_a", message_id="om_p"),)),
    ("image", {"image_key": "img_a"}, {}, "[image 3]", (ResourceRef(3, ResourceKind.IMAGE, "img_a", message_id="om_p"),)),
    ("file", {"file_key": "file_a", "file_name": "spec.pdf"}, {}, "[file 3: spec.pdf]",
     (ResourceRef(3, ResourceKind.FILE, "file_a", "spec.pdf", message_id="om_p"),)),
    ("interactive", {"title": "Build failed", "elements": [[{"tag": "text", "text": "step 3"}]]}, {},
     "Build failed\n    step 3", ()),
    ("interactive", {"elements": []}, {}, "[card message]", ()),
    ("text", "gone", {"deleted": True}, "[recalled]", ()),
])
def test_one_quoted_message_renders_by_type(msg_type: str, content: Any, fields: dict[str, Any], body: str,
                                            refs: tuple[ResourceRef, ...]) -> None:
    quote, found = lark_quote.build(QuoteKind.REPLY, "om_p", [_item("om_p", msg_type, content, **fields)],
                                    names={OPEN_ID: "Ada"}, after=2)
    assert _bodies(quote) == [body]
    assert quote.lines[0].startswith("[") and "] Ada: " in quote.lines[0]
    assert found == refs


def test_a_merged_forward_groups_children_nests_two_levels_and_downloads_through_the_container() -> None:
    items = [
        *_forward("om_f"),
        _item("om_a", content="topic", upper_message_id="om_f"),
        _item("om_b", content="answer", upper_message_id="om_f", parent_id="om_a", root_id="om_a"),
        _item("om_n1", "merge_forward", "", upper_message_id="om_f"),
        _item("om_c", "image", {"image_key": "img_c"}, upper_message_id="om_n1"),
        _item("om_n2", "merge_forward", "", upper_message_id="om_n1"),
        _item("om_d", content="too deep", upper_message_id="om_n2"),
    ]
    quote, refs = lark_quote.build(QuoteKind.FORWARD, "om_f", items)
    assert [line.partition("[")[0] for line in quote.lines] == ["", "  ", "", "  ", "  "]
    assert _bodies(quote) == ["topic", "answer", "[merged forward, 2 messages]", "[image 1]",
                              "[merged forward, not expanded]"]
    assert refs == (ResourceRef(1, ResourceKind.IMAGE, "img_c", message_id="om_f"),)
    assert (quote.message_type, quote.messages) == ("merge_forward", 3)


@pytest.mark.parametrize(("count", "size", "own", "shown", "cut"), [
    (1, 2_000, False, 1, lark_quote.MESSAGE_MAX),
    (1, 2_000, True, 1, lark_quote.OWN_MAX),
    (60, 10, False, lark_quote.BLOCK_MESSAGES, 0),
    (10, 1_500, False, 5, 0),
])
def test_quotes_are_bounded_per_message_and_per_block(count: int, size: int, own: bool, shown: int,
                                                      cut: int) -> None:
    sender = "cli_self" if own else OPEN_ID
    items = _forward("om_f", *(_item(f"om_{n}", content="x" * size, sender=sender, upper_message_id="om_f")
                               for n in range(count))) if count > 1 else [_item("om_f", content="x" * size,
                                                                               sender=sender)]
    quote, _ = lark_quote.build(QuoteKind.REPLY, "om_f", items, own=("cli_self",))
    bodies = _bodies(quote)
    assert len([body for body in bodies if body.startswith("x")]) == shown
    assert sum(len(line) for line in quote.lines) <= lark_quote.BLOCK_MAX + 100
    if cut:
        assert len(bodies[0]) == cut and bodies[0].endswith("…")
    if shown < count:
        assert quote.lines[-1] == f"… {count - shown} more not shown"


@pytest.mark.parametrize(("error", "reason"), [
    (TimeoutError(), "timeout"),
    (LarkCliError("lark-cli did not answer within 4 s", error_type="timeout"), "timeout"),
    (LarkCliError("The message is recalled.", code=230011), "recalled"),
    (LarkCliError("access denied (missing scope: im:message:readonly)", code=99991672),
     "missing permission im:message:readonly"),
    (LarkCliError("Bot is not in the chat", code=230002), "error 230002"),
])
def test_a_failed_read_becomes_one_line_saying_why(error: BaseException, reason: str) -> None:
    quote, refs = lark_quote.build(QuoteKind.REPLY, "om_p", [], error=lark_quote.failure(error))
    assert quote.lines == (f"[quoted message om_p could not be read: {reason}]",) and refs == ()


# --- The adapter, through the lark-cli stand-in ---


def _world(tmp_path: Path, **values: Any) -> None:
    path = tmp_path / "world.json"
    world = json.loads(path.read_text())
    world.update(values)
    path.write_text(json.dumps(world))


def _cli_event(**fields: Any) -> dict[str, Any]:
    return {"type": "im.message.receive_v1", "chat_id": "oc_group", "chat_type": "group", "message_id": "om_in",
            "message_type": "text", "sender_type": "user", "sender_id": OPEN_ID,
            "content": "@HelperBot what does this say?", "mentions": [BOT_MENTION], **fields}


def _calls(log: Path) -> list[list[str]]:
    return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []


def _reads(log: Path) -> list[list[str]]:
    return [argv for argv in _calls(log) if argv[:2] == ["api", "GET"] or "+messages-mget" in argv]


async def _received(tmp_path: Path, event: dict[str, Any]) -> list[Any]:
    adapter = LarkChannelAdapter(runtime_root=tmp_path, bot_open_id=BOT_OPEN_ID, bot_app_id="cli_main",
                                 allowlist=SenderAllowlist("channel-lark", frozenset({OPEN_ID})))
    bus = MessageBus(runtime_root=tmp_path)
    adapter._bus = bus
    try:
        await adapter._handle_lark_event(event)
        await asyncio.gather(*adapter._reactions._pending)
        return bus.journal.undelivered()
    finally:
        await adapter.stop()
        await bus.shutdown()


def test_a_reply_reads_its_parent_into_quoted_and_keeps_the_text(tmp_path: Path, lark_cli_stand_in: Path) -> None:
    _world(tmp_path, messages={"om_parent": [_item("om_parent", "image", {"image_key": "img_p"}, sender="ou_zyxwvut987")]},
           resources={"om_parent/img_p": {"base64": "iVBORw0KGgo="}})
    (message,) = asyncio.run(_received(tmp_path, _cli_event(reply_to="om_parent", root_id="om_parent")))
    assert message.payload["text"] == "what does this say?"
    assert references(message)[3] == "om_parent"  # gate routing reads the same reply id and text
    (quote,) = lark_quote.quotes(message.payload["quoted"])
    assert (quote.kind, quote.message_id, quote.sender) == (QuoteKind.REPLY, "om_parent", "Alan Turing")
    assert _bodies(quote) == ["[image 1]"]
    (attachment,) = message.payload["attachments"]
    assert attachment["state"] == "saved" and attachment["index"] == 1
    downloads = [argv for argv in _calls(lark_cli_stand_in) if "+messages-resources-download" in argv]
    assert downloads[0][downloads[0].index("--message-id") + 1] == "om_parent"


def test_a_raw_read_that_times_out_falls_back_to_the_rendered_read_once(
    tmp_path: Path, lark_cli_stand_in: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(lark_transport, "READ_FIRST_SECONDS", 0.5)
    rendered = "<forwarded_messages>\n[10:02] Bob: the plan\n![Image](img_f)\n</forwarded_messages>"
    _world(tmp_path, mget={"om_longslow": [{"message_id": "om_longslow", "msg_type": "merge_forward",
                                            "create_time": "2026-11-17 10:05",
                                            "sender": {"id": "ou_zyxwvut987", "name": "Alan Turing"},
                                            "content": rendered}]})
    (message,) = asyncio.run(_received(tmp_path, _cli_event(reply_to="om_longslow")))
    (quote,) = lark_quote.quotes(message.payload["quoted"])
    assert quote.sender == "Alan Turing" and quote.message_type == "merge_forward"
    assert "Bob: the plan" in quote.lines[0] and "[image 1]" in quote.lines[0]
    assert [argv[2] if argv[0] == "api" else argv[1] for argv in _reads(lark_cli_stand_in)] == [
        "/open-apis/im/v1/messages/om_longslow", "+messages-mget"]


@pytest.mark.parametrize(("parent", "reason"), [("om_unknown", "error 230001"),
                                                ("om_noscope", "missing permission im:message:readonly")])
def test_an_unreadable_parent_still_publishes_the_message(tmp_path: Path, lark_cli_stand_in: Path, parent: str,
                                                          reason: str) -> None:
    (message,) = asyncio.run(_received(tmp_path, _cli_event(reply_to=parent)))
    assert message.payload["text"] == "what does this say?"
    (quote,) = lark_quote.quotes(message.payload["quoted"])
    assert quote.lines == (f"[quoted message {parent} could not be read: {reason}]",)


def test_a_merged_forward_in_a_direct_chat_reaches_the_manager(tmp_path: Path, lark_cli_stand_in: Path) -> None:
    _world(tmp_path, messages={"om_in": _forward("om_in", _item("om_a", content="topic", upper_message_id="om_in"))})
    (message,) = asyncio.run(_received(tmp_path, _cli_event(
        chat_id="oc_dm", chat_type="p2p", message_type="merge_forward", content="Merged and Forwarded Message",
        mentions=[])))
    assert message.payload["text"] == ""
    (quote,) = lark_quote.quotes(message.payload["quoted"])
    assert quote.kind is QuoteKind.FORWARD and _bodies(quote) == ["topic"]
    assert not [argv for argv in _calls(lark_cli_stand_in) if "+messages-reply" in argv
                or "+messages-send" in argv]  # no "not supported" answer


@pytest.mark.parametrize("event", [
    {"message_type": "merge_forward", "content": "Merged and Forwarded Message", "mentions": []},
    {"content": "/stop", "reply_to": "om_parent"},
])
def test_unaddressed_forwards_and_stops_read_nothing(tmp_path: Path, lark_cli_stand_in: Path,
                                                    event: dict[str, Any]) -> None:
    messages = asyncio.run(_received(tmp_path, _cli_event(**event)))
    assert [message.payload["text"] for message in messages] == ([] if event.get("message_type") else ["/stop"])
    assert all("quoted" not in message.payload for message in messages)
    assert _reads(lark_cli_stand_in) == []


def test_a_reply_inside_a_thread_also_reads_its_first_message_and_latest_replies(
    tmp_path: Path, lark_cli_stand_in: Path,
) -> None:
    replies = [_item(f"om_r{n}", content=f"reply {n}") for n in range(12, 0, -1)]  # newest first
    _world(tmp_path, messages={"om_r5": [_item("om_r5", content="reply 5")], "om_root": [_item("om_root", content="start")]},
           threads={"omt_1": [_item("om_in"), *replies]})
    (message,) = asyncio.run(_received(tmp_path, _cli_event(reply_to="om_r5", root_id="om_root", thread_id="omt_1")))
    reply, thread = lark_quote.quotes(message.payload["quoted"])
    assert _bodies(reply) == ["reply 5"]
    assert thread.kind is QuoteKind.THREAD and thread.message_id == "om_root"
    assert _bodies(thread) == ["start", *(f"reply {n}" for n in (2, 3, 4, *range(6, 13)))]
    assert all(line.startswith("  [") for line in thread.lines[1:])


# --- The bot-token transport, through the local platform ---


def test_a_bot_channel_reply_in_a_thread_is_quoted_and_answered_in_the_thread(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(lark, "observe_chat", AsyncMock())
    monkeypatch.setattr(lark_transport, "READ_FIRST_SECONDS", 0.3)

    async def main() -> None:
        async with FakeLarkPlatform() as fake:
            fake.users = [{"open_id": "ou_sender", "name": "Ada Lovelace"}, {"open_id": OPEN_ID, "name": "Bob"}]
            fake.messages["om_parent"] = [_item("om_parent", content="the parent")]
            fake.slow_once["/open-apis/im/v1/messages/om_parent"] = 1.0  # the first read outlasts its try
            bus = MessageBus(runtime_root=tmp_path)
            bot = fake.bot["open_id"]
            adapter = make_adapter({"app_id": fake.app_id}, "test-secret", runtime_root=tmp_path,
                allowlist=SenderAllowlist("channel-lark_bot", frozenset({"ou_sender"})), on_rejected=None,
                bot_open_id=bot, base_url=fake.base_url,
                resolve_chat=lambda *_: RouteDecision(RouteAction.ADMIT, "manager"))
            adapter._bus = bus
            try:
                async with aclosing(adapter._transport.events()) as events:
                    pending = asyncio.create_task(anext(events))
                    await fake.push_message(text="@_user_1 explain", sender="ou_sender", chat_type="group",
                                            message_id="om_ask", parent_id="om_parent", root_id="om_parent",
                                            thread_id="omt_thread",
                                            mentions=[{"id": {"open_id": bot}, "key": "@_user_1", "name": "Helper"}])
                    event = await asyncio.wait_for(pending, 2)
                    assert event["thread_id"] == "omt_thread"
                    await adapter._handle_lark_event(event)
                message = bus.get_queue("manager").get_nowait()
                assert message.payload["text"] == "explain"
                (quote,) = lark_quote.quotes(message.payload["quoted"])
                assert _bodies(quote) == ["the parent"] and quote.sender == "Bob"
                reads = [request for request in fake.requests if request["path"] == "/open-apis/im/v1/messages/om_parent"]
                assert len(reads) == 2
                await adapter.deliver(MessageEnvelope.create(
                    source=message.target, target=message.source, message_type=MessageType.AGENT_REPLY,
                    correlation_id=message.correlation_id, payload={"text": "answer"}))
                (answer,) = [sent for sent in fake.sent if sent["kind"] == "reply"]
                assert answer["parent"] == "om_ask"
                body = [request["body"] for request in fake.requests if request["path"].endswith("/om_ask/reply")]
                assert body[-1].get("reply_in_thread") is True
            finally:
                await adapter.stop()
                await bus.shutdown()

    asyncio.run(asyncio.wait_for(main(), 15))
