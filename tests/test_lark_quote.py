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
from looper.agents.channels.lark_inbox import LarkInbox
from test_lark_transport import BOT_MENTION as BOT_MENTION_STUB, StubTransport, _adapter, _event

STAMP = "1763350000000"
PNG = b"\x89PNG\r\n\x1a\n"


def _item(message_id: str, msg_type: str = "text", content: Any = "hi", *, sender: str = OPEN_ID,
          sender_type: str = "user", **fields: Any) -> dict[str, Any]:
    body = json.dumps({"text": content} if msg_type == "text" else content)
    return {"message_id": message_id, "msg_type": msg_type, "create_time": STAMP, "body": {"content": body},
            "sender": {"id": sender, "id_type": "open_id", "sender_type": sender_type}, **fields}


def _tasks(adapter: Any) -> list[Any]:
    """The adapter's messages still being read in the background."""
    return [delivery.task for delivery in adapter._unsettled.values() if delivery.task is not None]


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


def test_a_merged_forward_groups_children_nests_two_levels_and_downloads_through_each_child() -> None:
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
    assert refs == (ResourceRef(1, ResourceKind.IMAGE, "img_c", message_id="om_c"),)  # the child's own keys
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
        await asyncio.gather(*_tasks(adapter), *adapter._reactions._pending)
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


@pytest.mark.parametrize("parent", ["om_longslow", "om_longservertimeout"])
def test_a_raw_read_that_times_out_falls_back_to_the_rendered_read_once(
    tmp_path: Path, lark_cli_stand_in: Path, monkeypatch: pytest.MonkeyPatch, parent: str,
) -> None:
    monkeypatch.setattr(lark_transport, "READ_FIRST_SECONDS", 1.5)
    rendered = "<forwarded_messages>\n[10:02] Bob: the plan\n[Image: img_f]\n</forwarded_messages>"
    _world(tmp_path, mget={parent: [{"message_id": parent, "msg_type": "merge_forward",
                                     "create_time": "2026-11-17 10:05",
                                     "sender": {"id": "ou_zyxwvut987", "name": "Alan Turing"},
                                     "content": rendered}]})
    (message,) = asyncio.run(_received(tmp_path, _cli_event(reply_to=parent)))
    (quote,) = lark_quote.quotes(message.payload["quoted"])
    assert quote.sender == "Alan Turing" and quote.message_type == "merge_forward"
    assert "Bob: the plan" in quote.lines[0] and "[image 1]" in quote.lines[0]
    # A stand-in killed at its first try may not have logged it yet; the rendered read comes once, last.
    reads = [argv[2] if argv[0] == "api" else argv[1] for argv in _reads(lark_cli_stand_in)]
    assert reads[-1] == "+messages-mget" and reads.count("+messages-mget") == 1
    assert set(reads[:-1]) <= {f"/open-apis/im/v1/messages/{parent}"}
    downloads = [argv for argv in _calls(lark_cli_stand_in) if "+messages-resources-download" in argv]
    assert downloads[0][downloads[0].index("--message-id") + 1] == parent  # the rendering shows the forward's copies


@pytest.mark.parametrize(("parent", "reason"), [("om_unknown", "error 230001"),
                                                ("om_noscope", "missing permission im:message:readonly"),
                                                ("om_lostservertimeout", "timeout")])
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
                await asyncio.gather(*_tasks(adapter))
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


# --- Reading off the channel's ingress: other chats, stops and a chat's order ---


class _SlowReads(StubTransport):
    """Reads that wait until ``release`` is set."""

    def __init__(self) -> None:
        super().__init__()
        self.release = asyncio.Event()

    async def get_message(self, message_id: str, *, timeout: float) -> list[dict[str, Any]]:
        await self.release.wait()
        return [_item(message_id, content="the parent")]


def _texts(bus: MessageBus) -> list[str]:
    return [message.payload["text"] for message in bus.journal.undelivered()]


def test_a_slow_quote_holds_up_only_its_own_chat_and_keeps_its_order(tmp_path: Path) -> None:
    async def main() -> None:
        transport = _SlowReads()
        adapter = _adapter(tmp_path, transport)
        bus = MessageBus(runtime_root=tmp_path)
        adapter._bus = bus
        handle = lambda **fields: asyncio.wait_for(adapter._handle_lark_event(_event(**fields)), 2)  # noqa: E731
        try:
            await handle(message_id="om_a1", chat_id="oc_a", content="what does it say?", reply_to="om_parent")
            await handle(message_id="om_a2", chat_id="oc_a", content="and then?")
            await handle(message_id="om_b1", chat_id="oc_b", content="plain")
            await handle(message_id="om_b2", chat_id="oc_b", content="/stop")
            assert _texts(bus) == ["plain", "/stop"]  # chat b did not wait; om_a2 waits behind om_a1
            transport.release.set()
            await asyncio.wait_for(asyncio.gather(*_tasks(adapter)), 2)
            assert _texts(bus) == ["plain", "/stop", "what does it say?", "and then?"]
            (quote,) = lark_quote.quotes(bus.journal.undelivered()[2].payload["quoted"])
            assert _bodies(quote) == ["the parent"]
        finally:
            await adapter.stop()
            await bus.shutdown()

    asyncio.run(main())


def test_a_stop_withdraws_its_chats_messages_still_being_read_without_waiting(tmp_path: Path) -> None:
    """They go first, typed as withdrawn: the Manager never takes them as turns or gate replies."""
    async def main() -> None:
        transport = _SlowReads()
        adapter = _adapter(tmp_path, transport)
        bus = MessageBus(runtime_root=tmp_path)
        adapter._bus = bus
        try:
            await adapter._handle_lark_event(_event(message_id="om_a1", content="explain", reply_to="om_parent"))
            await asyncio.wait_for(adapter._handle_lark_event(_event(message_id="om_a2", content="/stop")), 2)
            assert _texts(bus) == ["explain", "/stop"] and not adapter._unsettled
            withdrawn, stop = bus.journal.undelivered()
            assert withdrawn.payload.get("withdrawn") is True and "quoted" not in withdrawn.payload
            assert "withdrawn" not in stop.payload
        finally:
            await adapter.stop()
            await bus.shutdown()

    asyncio.run(main())


# --- Delivery failures, limits and internal errors ---


async def _with_inbox(tmp_path: Path, transport: StubTransport) -> tuple[LarkChannelAdapter, MessageBus]:
    adapter = _adapter(tmp_path, transport)
    adapter._inbox = await LarkInbox.open(tmp_path / "inbox.sqlite3")
    await adapter._inbox.checkpoint(1.0)
    transport.consumer_started = asyncio.Event()  # a healthy consumer: only unsettled messages keep stop unclean
    transport.consumer_started.set()
    bus = MessageBus(runtime_root=tmp_path)
    adapter._bus = bus
    return adapter, bus


@pytest.mark.parametrize(("fails", "step", "delivered"), [
    (2, "publish", True), (99, "publish", False), (99, "record", False),
])
def test_a_message_not_published_or_recorded_holds_its_chat_and_the_boundary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fails: int, step: str, delivered: bool,
) -> None:
    monkeypatch.setattr(lark, "_DELIVERY_RETRY_SECONDS", (0.0,) * 4)

    async def main() -> float | None:
        transport = _SlowReads()
        transport.release.set()
        adapter, bus = await _with_inbox(tmp_path, transport)
        tries = {"left": fails}
        target = bus.publish if step == "publish" else adapter._inbox.finish

        async def flaky(*args: Any, **kwargs: Any) -> Any:
            first = args[0]
            if (first if step == "record" else first.payload["channel_context"]["message_id"]) == "om_a1" \
                    and tries["left"] > 0:
                tries["left"] -= 1
                raise OSError("the journal cannot be written")
            return await target(*args, **kwargs)

        if step == "publish":
            bus.publish = flaky
        else:
            adapter._inbox.finish = flaky
        try:
            await adapter._handle_lark_event(_event(message_id="om_a1", chat_id="oc_a", reply_to="om_parent"))
            await adapter._handle_lark_event(_event(message_id="om_a2", chat_id="oc_a", content="later"))
            await adapter._handle_lark_event(_event(message_id="om_b1", chat_id="oc_b", content="elsewhere"))
            await asyncio.wait_for(asyncio.gather(*_tasks(adapter)), 5)
            await adapter._handle_lark_event(_event(message_id="om_a3", chat_id="oc_a", content="after"))
            by_chat = {chat: [m.payload["text"] for m in bus.journal.undelivered()
                              if m.payload["channel_context"]["chat_id"] == chat] for chat in ("oc_a", "oc_b")}
            assert by_chat == {"oc_b": ["elsewhere"], "oc_a": ["hello", "later", "after"] if delivered else (
                [] if step == "publish" else ["hello"])}
            for message_id, recorded in (("om_a1", delivered), ("om_a2", delivered), ("om_a3", delivered),
                                         ("om_b1", True)):
                assert await adapter._inbox.processed(message_id) is recorded
            assert await adapter._settled("oc_a") is delivered  # catch-up and the bot poll keep their windows
            assert bool(adapter._unsettled) is not delivered
        finally:
            await adapter.stop()
            await bus.shutdown()
        reopened = await LarkInbox.open(tmp_path / "inbox.sqlite3")
        try:
            return reopened.boundary
        finally:
            await reopened.close()

    boundary = asyncio.run(main())
    assert (boundary != 1.0) is delivered  # a clean stop saves its time; a held message keeps the old boundary


@pytest.mark.parametrize("limit", ["_CHAT_PENDING_MAX", "_PENDING_MAX"])
def test_past_the_waiting_limit_a_message_goes_without_its_quotes_read_and_in_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, limit: str,
) -> None:
    monkeypatch.setattr(lark, limit, 1)

    async def main() -> None:
        transport = _SlowReads()
        reads: list[str] = []
        read = transport.get_message

        async def counted(message_id: str, *, timeout: float) -> list[dict[str, Any]]:
            reads.append(message_id)
            return await read(message_id, timeout=timeout)

        transport.get_message = counted
        adapter = _adapter(tmp_path, transport)
        bus = MessageBus(runtime_root=tmp_path)
        adapter._bus = bus
        second_chat = "oc_a" if limit == "_CHAT_PENDING_MAX" else "oc_b"
        try:
            await adapter._handle_lark_event(_event(message_id="om_1", chat_id="oc_a", content="first",
                                                    reply_to="om_p1"))
            await adapter._handle_lark_event(_event(message_id="om_2", chat_id=second_chat, content="second",
                                                    reply_to="om_p2"))
            await asyncio.sleep(0)
            transport.release.set()
            await asyncio.wait_for(asyncio.gather(*_tasks(adapter)), 2)
            assert reads == ["om_p1"]
            messages = bus.journal.undelivered()
            # In one chat the second waits for the first; in another it need not.
            assert [m.payload["text"] for m in messages] == (["first", "second"] if second_chat == "oc_a"
                                                             else ["second", "first"])
            second = next(m for m in messages if m.payload["text"] == "second")
            (quote,) = lark_quote.quotes(second.payload["quoted"])
            assert quote.lines == ("[quoted message om_p2 could not be read: busy]",)
        finally:
            await adapter.stop()
            await bus.shutdown()

    asyncio.run(main())


def test_an_internal_error_reading_a_quote_still_delivers_a_bare_forward(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def broken(self: Any, delivery: Any) -> Any:
        raise RuntimeError("a renderer bug")

    monkeypatch.setattr(LarkChannelAdapter, "_quote", broken)

    async def main() -> None:
        adapter = _adapter(tmp_path, _SlowReads())
        bus = MessageBus(runtime_root=tmp_path)
        adapter._bus = bus
        try:
            await adapter._handle_lark_event(_event(message_type="merge_forward", content="Merged and Forwarded Message"))
            await asyncio.wait_for(asyncio.gather(*_tasks(adapter)), 2)
            (message,) = bus.journal.undelivered()
            assert message.payload["text"] == ""
            (quote,) = lark_quote.quotes(message.payload["quoted"])
            assert quote.lines == ("[quoted message om_in could not be read: internal error]",)
        finally:
            await adapter.stop()
            await bus.shutdown()

    asyncio.run(main())


def test_only_the_latest_thread_messages_are_kept(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(lark, "_THREAD_MESSAGES_KEPT", 2)

    async def main() -> None:
        adapter = _adapter(tmp_path, StubTransport())
        bus = MessageBus(runtime_root=tmp_path)
        adapter._bus = bus
        try:
            for n in range(3):
                await adapter._handle_lark_event(_event(message_id=f"om_t{n}", chat_type="group", chat_id="oc_team",
                                                        mentions=[BOT_MENTION_STUB], thread_id="omt_1"))
            assert list(adapter._thread_messages) == ["om_t1", "om_t2"]
        finally:
            await adapter.stop()
            await bus.shutdown()

    asyncio.run(main())
