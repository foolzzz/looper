"""The shared Lark adapter through an in-memory transport; no platform or CLI calls."""

from __future__ import annotations

import asyncio
import shutil
from collections.abc import AsyncIterator, Iterable
from pathlib import Path
from typing import Any

import pytest

from looper.agents.bus import MessageBus
from looper.agents.channels.allowlist import SenderAllowlist
from looper.agents.channels.lark import LarkChannelAdapter
from looper.agents.channels.lark_cli import LarkCliError
from looper.agents.channels.lark_transport import CliTransport
from looper.agents.message import MANAGER_ADDRESS, MessageEnvelope, MessageType, channel_address
from looper.agents.task_notice import NotifyTarget, TargetKind

OPEN_ID = "ou_abcdefgh12"
OTHER_ID = "ou_otheruser12"
BOT_ID = "ou_helperbot12"
BOT_MENTION = {"id": BOT_ID, "key": "@_user_1", "name": "HelperBot"}
USER = NotifyTarget(TargetKind.USER, OPEN_ID)
GROUP = NotifyTarget(TargetKind.CHAT, "oc_team", mention=OPEN_ID)


class StubTransport:
    """Record sends and hold an event stream open until the adapter stops it."""

    def __init__(self, *, available: bool = True, stream_failure: bool = False) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.can_run = available
        self.stream_failure = stream_failure
        self.stream_count = 0
        self.third_stream = asyncio.Event()
        self.stream_closed = asyncio.Event()
        self.closed = False
        self.error: LarkCliError | None = None
        self.message_id: str | None = "om_sent"

    async def _call(self, method: str, **kwargs: Any) -> str | None:
        self.calls.append((method, {key: value for key, value in kwargs.items() if value is not None}))
        if self.error:
            raise self.error
        return self.message_id

    async def send(self, *, chat_id: str | None = None, user_id: str | None = None,
                   text: str | None = None, markdown: str | None = None,
                   idempotency_key: str | None = None, return_id: bool = False) -> str | None:
        return await self._call("send", chat_id=chat_id, user_id=user_id, text=text, markdown=markdown,
                                idempotency_key=idempotency_key, **({"return_id": True} if return_id else {}))

    async def reply(self, message_id: str, *, text: str | None = None, markdown: str | None = None,
                    idempotency_key: str | None = None, return_id: bool = False) -> str | None:
        return await self._call("reply", message_id=message_id, text=text, markdown=markdown,
                                idempotency_key=idempotency_key, **({"return_id": True} if return_id else {}))

    async def edit(self, message_id: str, *, text: str) -> None:
        await self._call("edit", message_id=message_id, text=text)

    async def react(self, message_id: str) -> None:
        await self._call("react", message_id=message_id)

    async def events(self) -> AsyncIterator[dict[str, Any]]:
        self.stream_count += 1
        if self.stream_count < 3:
            if self.stream_failure:
                raise ConnectionError("the connection broke")
            return
        self.third_stream.set()
        try:
            await asyncio.Event().wait()
            yield {}
        finally:
            self.stream_closed.set()

    async def names(self, open_ids: Iterable[str]) -> dict[str, str]:
        await self._call("names", open_ids=list(open_ids))
        return {OPEN_ID: "Ada Lovelace"}

    def available(self) -> bool:
        return self.can_run

    async def aclose(self) -> None:
        self.closed = True


def _adapter(tmp_path: Path, transport: StubTransport, **kwargs: Any) -> LarkChannelAdapter:
    return LarkChannelAdapter(runtime_root=tmp_path, channel_type="lark_bot", transport=transport,
                              allowlist=SenderAllowlist("channel-lark_bot", frozenset({OPEN_ID})),
                              bot_open_id=BOT_ID, **kwargs)


def _event(**fields: Any) -> dict[str, Any]:
    return {"type": "im.message.receive_v1", "chat_id": "oc_dm", "chat_type": "p2p", "message_id": "om_in",
            "message_type": "text", "sender_type": "user", "sender_id": OPEN_ID, "content": "hello", **fields}


def _envelope(correlation_id: str, message_type: MessageType = MessageType.AGENT_REPLY,
              **payload: Any) -> MessageEnvelope:
    return MessageEnvelope.create(source=MANAGER_ADDRESS, target=channel_address("lark_bot"), message_type=message_type,
                                  correlation_id=correlation_id, payload=payload)


@pytest.mark.parametrize("printed", ["", "sent, without JSON"])
@pytest.mark.parametrize("reply", [False, True])
def test_cli_ordinary_messages_ignore_successful_stdout(
    lark_cli_stand_in: Path, printed: str, reply: bool,
) -> None:
    executable = Path(f"{shutil.which('lark-cli')}.py")
    executable.write_text(executable.read_text().replace("print(json.dumps(value))", f"print({printed!r}, end='')"))

    async def main() -> None:
        transport = CliTransport()
        if reply:
            assert await transport.reply("om_parent", text="hello") is None
        else:
            assert await transport.send(chat_id="oc_dm", text="hello") is None

    asyncio.run(main())
    assert len(lark_cli_stand_in.read_text().splitlines()) == 1


@pytest.mark.parametrize("reply", [False, True])
def test_cli_posts_and_status_require_json_even_when_stdout_is_empty(
    tmp_path: Path, lark_cli_stand_in: Path, reply: bool,
) -> None:
    executable = Path(f"{shutil.which('lark-cli')}.py")
    executable.write_text(executable.read_text().replace("print(json.dumps(value))", "print('', end='')"))

    async def main() -> None:
        adapter = LarkChannelAdapter(runtime_root=tmp_path,
                                     allowlist=SenderAllowlist("channel-lark", frozenset({OPEN_ID})))
        adapter._correlation_chat["c1"] = "oc_dm"
        if reply:
            adapter._group_message["c1"] = "om_parent"
        try:
            receipt = await adapter.post(USER, "question", reply_to="om_parent" if reply else None)
            assert not receipt.success
            assert "not JSON" in (receipt.error or "")
            with pytest.raises(LarkCliError, match="not JSON"):
                await adapter._send_status("c1", "thinking")
        finally:
            await adapter.stop()

    asyncio.run(main())


@pytest.mark.parametrize("group", [False, True])
def test_bot_messages_keep_their_channel_and_route_the_answer(tmp_path: Path, group: bool) -> None:
    async def main() -> None:
        transport = StubTransport()
        adapter = _adapter(tmp_path, transport)
        bus = MessageBus(runtime_root=tmp_path)
        adapter._bus = bus
        event = _event(chat_id="oc_team" if group else "oc_dm", chat_type="group" if group else "p2p",
                       content="@HelperBot hello" if group else "hello", mentions=[BOT_MENTION] if group else [],
                       reply_to="om_card", root_id="om_root")
        try:
            await adapter._handle_lark_event(event)
            (message,) = bus.journal.undelivered()
            assert message.source == channel_address("lark_bot")
            assert message.metadata["origin_channel_id"] == "channel-lark_bot"
            assert message.payload["text"] == "hello"
            context = message.payload["channel_context"]
            assert context["reply_to"] == "om_card" and context["root_id"] == "om_root"
            if group:
                assert context["sender_name"] == "Ada Lovelace"
                assert ("names", {"open_ids": [OPEN_ID]}) in transport.calls
            await asyncio.gather(*adapter._reactions._pending)
            assert ("react", {"message_id": "om_in"}) in transport.calls
            receipt = await adapter.deliver(_envelope(message.correlation_id, text="answer"))
            assert receipt.success and receipt.channel == "lark_bot"
            destination = ("reply", {"message_id": "om_in", "markdown": "answer"}) if group else (
                "send", {"chat_id": "oc_dm", "markdown": "answer"})
            assert destination in transport.calls
        finally:
            await adapter.stop()
            await bus.shutdown()

    asyncio.run(main())


@pytest.mark.parametrize(("group", "mention", "sender", "rejected"), [
    (False, False, OTHER_ID, [(OTHER_ID, None)]),
    (True, True, OTHER_ID, [(OTHER_ID, "oc_team")]),
    (True, False, OTHER_ID, []),
    (True, False, OPEN_ID, []),
])
def test_unlisted_and_unaddressed_messages_have_no_side_effects(
    tmp_path: Path, group: bool, mention: bool, sender: str, rejected: list[tuple[str, str | None]],
) -> None:
    async def main() -> None:
        transport = StubTransport()
        seen: list[tuple[str, str | None]] = []

        async def record(sender_id: str, chat_id: str | None) -> None:
            seen.append((sender_id, chat_id))

        adapter = _adapter(tmp_path, transport, on_rejected=record)
        bus = MessageBus(runtime_root=tmp_path)
        adapter._bus = bus
        try:
            await adapter._handle_lark_event(_event(sender_id=sender, chat_type="group" if group else "p2p",
                                                   chat_id="oc_team" if group else "oc_dm",
                                                   content="@HelperBot hello" if mention else "hello",
                                                   mentions=[BOT_MENTION] if mention else []))
            assert bus.journal.undelivered() == []
            assert transport.calls == []
            assert seen == rejected
        finally:
            await adapter.stop()
            await bus.shutdown()

    asyncio.run(main())


@pytest.mark.parametrize("group", [False, True])
def test_status_is_sent_once_then_edited_while_progress_and_answer_stay_messages(tmp_path: Path, group: bool) -> None:
    async def main() -> None:
        transport = StubTransport()
        adapter = _adapter(tmp_path, transport)
        adapter._correlation_chat["c1"] = "oc_team" if group else "oc_dm"
        if group:
            adapter._group_message["c1"] = "om_in"
        try:
            receipt = await adapter.deliver(_envelope("c1", MessageType.STATUS_UPDATE, phase="thinking",
                                                       activity="thinking"))
            assert receipt.success and receipt.channel == "lark_bot"
            method, fields = transport.calls[0]
            assert method == ("reply" if group else "send")
            assert fields.get("message_id" if group else "chat_id") == ("om_in" if group else "oc_dm")
            assert fields["text"].startswith("⏳")
            assert fields["return_id"] is True
            receipt = await adapter.deliver(_envelope("c1", MessageType.STATUS_UPDATE, text="task progress"))
            assert receipt.success and receipt.channel == "lark_bot"
            assert transport.calls[-1] == ("send", {"chat_id": "oc_team" if group else "oc_dm",
                                                    "markdown": "task progress"})
            await adapter.deliver(_envelope("c1", text="done"))
            assert transport.calls[-1][0] == "edit"
            assert transport.calls[-1][1]["message_id"] == "om_sent"
            assert transport.calls[-1][1]["text"].startswith("✅")
        finally:
            await adapter.stop()

    asyncio.run(main())


@pytest.mark.parametrize(("target", "reply_to", "method", "destination", "markdown"), [
    (USER, None, "send", {"user_id": OPEN_ID}, "gate question"),
    (GROUP, None, "send", {"chat_id": "oc_team"}, f'<at user_id="{OPEN_ID}"></at> gate question'),
    (GROUP, "om_parent", "reply", {"message_id": "om_parent"}, "gate question"),
])
def test_posts_keep_the_target_reply_and_idempotency_key(
    tmp_path: Path, target: NotifyTarget, reply_to: str | None, method: str, destination: dict[str, str], markdown: str,
) -> None:
    async def main() -> None:
        transport = StubTransport()
        adapter = _adapter(tmp_path, transport)
        receipt = await adapter.post(target, "gate question", reply_to=reply_to, idempotency_key="gate-42")
        assert receipt.success and receipt.external_ids == ("om_sent",)
        assert transport.calls == [(method, {**destination, "markdown": markdown, "idempotency_key": "gate-42",
                                             "return_id": True})]
        await adapter.stop()

    asyncio.run(main())


def test_notifications_and_task_notices_use_the_transport(tmp_path: Path) -> None:
    async def main() -> None:
        transport = StubTransport()
        adapter = _adapter(tmp_path, transport)
        receipt = await adapter.notify(OPEN_ID, "scheduled work")
        assert receipt.success and receipt.channel == "lark_bot"
        assert transport.calls[-1] == ("send", {"user_id": OPEN_ID, "text": "scheduled work"})
        for target in (USER, GROUP):
            receipt = await adapter.deliver(_envelope("notice", MessageType.CHAT_NOTICE, channel="lark_bot",
                                                       kind=target.kind.value, chat=target.id, mention=target.mention,
                                                       text="task finished"))
            assert receipt.success and receipt.channel == "lark_bot"
            body = f'<at user_id="{OPEN_ID}"></at> task finished' if target is GROUP else "task finished"
            destination = {"chat_id": "oc_team"} if target is GROUP else {"user_id": OPEN_ID}
            assert transport.calls[-1] == ("send", {**destination, "markdown": body})
        await adapter.stop()

    asyncio.run(main())


@pytest.mark.parametrize(("code", "reply_to", "gone"), [(230011, "om_parent", True), (999, "om_parent", False),
                                                         (230011, None, False)])
def test_platform_reply_errors_report_whether_the_parent_is_gone(
    tmp_path: Path, code: int, reply_to: str | None, gone: bool,
) -> None:
    from looper.agents.channels.lark_api import LarkApiError

    async def main() -> None:
        transport = StubTransport()
        transport.error = LarkApiError("the platform refused the message", code=code)
        adapter = _adapter(tmp_path, transport)
        receipt = await adapter.post(USER, "gate question", reply_to=reply_to)
        assert not receipt.success and receipt.parent_gone is gone
        assert "the platform refused" in (receipt.error or "")
        await adapter.stop()

    asyncio.run(main())


@pytest.mark.parametrize(("code", "send_error", "success"), [
    (230011, None, True), (999, None, False), (None, None, False), (230011, "send refused", False),
])
def test_recovered_progress_falls_back_only_when_the_original_message_is_gone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, code: int | None, send_error: str | None, success: bool,
) -> None:
    async def main() -> None:
        transport = StubTransport()
        call = transport._call

        async def failing_call(method: str, **kwargs: Any) -> str | None:
            result = await call(method, **kwargs)
            if method == "reply":
                raise LarkCliError("reply refused", code=code)
            if send_error:
                raise LarkCliError(send_error)
            return result

        monkeypatch.setattr(transport, "_call", failing_call)
        adapter = _adapter(tmp_path, transport)
        envelope = MessageEnvelope.create(
            source=MANAGER_ADDRESS, target=channel_address("lark_bot"), message_type=MessageType.STATUS_UPDATE,
            correlation_id="recovered", payload={"kind": "progress", "text": "task progress"},
            metadata={"reply_route": {"chat_id": "oc_original", "reply_to": "om_recalled"}},
        )
        try:
            receipt = await adapter.deliver(envelope)
            assert receipt.success is success
            assert receipt.error == (None if success else send_error or "reply refused")
            expected = [("reply", {"message_id": "om_recalled", "markdown": "task progress"})]
            if code == 230011:
                expected.append(("send", {"chat_id": "oc_original", "markdown": "task progress"}))
            assert transport.calls == expected
            assert not adapter._correlation_chat and not adapter._group_message
        finally:
            await adapter.stop()

    asyncio.run(main())


def test_a_post_without_a_message_id_is_not_successful(tmp_path: Path) -> None:
    async def main() -> None:
        transport = StubTransport()
        transport.message_id = None
        adapter = _adapter(tmp_path, transport)
        receipt = await adapter.post(USER, "gate question")
        assert not receipt.success and receipt.external_ids == () and receipt.error
        await adapter.stop()

    asyncio.run(main())


@pytest.mark.parametrize("operation", ["answer", "group_answer", "notify"])
def test_transport_send_failures_are_returned_as_receipts(tmp_path: Path, operation: str) -> None:
    async def main() -> None:
        transport = StubTransport()
        transport.error = LarkCliError("the platform refused the message")
        adapter = _adapter(tmp_path, transport)
        adapter._correlation_chat["c1"] = "oc_dm"
        if operation == "group_answer":
            adapter._group_message["c1"] = "om_in"
        receipt = (await adapter.notify(OPEN_ID, "notice") if operation == "notify" else
                   await adapter.deliver(_envelope("c1", text="answer")))
        assert not receipt.success and receipt.channel == "lark_bot"
        assert "the platform refused" in (receipt.error or "")
        await adapter.stop()

    asyncio.run(main())


@pytest.mark.parametrize("failure", [False, True])
def test_streams_restart_after_ending_or_raising_and_stop_closes_them(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: bool,
) -> None:
    monkeypatch.setattr(LarkChannelAdapter, "_RESTART_SECONDS", 0.01)

    async def main() -> None:
        transport = StubTransport(stream_failure=failure)
        adapter = _adapter(tmp_path, transport)
        bus = MessageBus(runtime_root=tmp_path)
        try:
            await adapter.start(bus)
            await asyncio.wait_for(transport.third_stream.wait(), 1)
            assert transport.stream_count == 3
            health = adapter.healthcheck()
            assert health["channel"] == "lark_bot" and health["healthy"]
            assert "transport" in health and "lark_cli_available" not in health
        finally:
            await adapter.stop()
            await bus.shutdown()
        assert transport.closed and transport.stream_closed.is_set()
        assert adapter._collector_task is not None and adapter._collector_task.done()
        assert adapter._outbound_task is not None and adapter._outbound_task.done()
        assert not adapter.healthcheck()["healthy"]
        assert transport.stream_count == 3

    asyncio.run(main())


def test_an_unavailable_transport_does_not_start_the_adapter(tmp_path: Path) -> None:
    async def main() -> None:
        transport = StubTransport(available=False)
        adapter = _adapter(tmp_path, transport)
        bus = MessageBus(runtime_root=tmp_path)
        await adapter.start(bus)
        assert adapter._collector_task is None and adapter._outbound_task is None
        assert not adapter.healthcheck()["healthy"] and transport.stream_count == 0
        await adapter.stop()
        await bus.shutdown()
        assert transport.closed

    asyncio.run(main())


def test_one_event_handler_failure_does_not_drop_later_events(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    async def main() -> None:
        transport = StubTransport()
        handled: list[str] = []

        async def events() -> AsyncIterator[dict[str, Any]]:
            yield {"message_id": "broken"}
            yield {"message_id": "next"}

        async def handle(event: dict[str, Any]) -> None:
            handled.append(event["message_id"])
            if event["message_id"] == "broken":
                raise ValueError("one malformed message")

        monkeypatch.setattr(transport, "events", events)
        adapter = _adapter(tmp_path, transport)
        monkeypatch.setattr(adapter, "_handle_lark_event", handle)
        adapter._running = True
        await adapter._run_event_consumer()
        assert handled == ["broken", "next"]
        await adapter.stop()

    asyncio.run(main())


def test_bot_transport_delegates_calls_and_reuses_seen_events_across_connections(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from looper.agents.channels import lark_api, lark_stream
    from looper.agents.channels.lark_bot import BotTransport

    transport_calls: list[tuple[str, dict[str, Any]]] = []
    connections: list[tuple[Any, Any, Any, Any]] = []
    made: list[tuple[str, str, Any]] = []

    class Api(StubTransport):
        def __init__(self, app_id: str, app_secret: str, *, base_url: str) -> None:
            super().__init__()
            self.calls = transport_calls
            made.append((app_id, app_secret, base_url))

    class Seen:
        pass

    async def events(api: Any, *, seen: Any, admit: Any, stats: Any) -> AsyncIterator[dict[str, Any]]:
        connections.append((api, seen, admit, stats))
        yield _event()

    monkeypatch.setattr(lark_api, "LarkApi", Api)
    monkeypatch.setattr(lark_stream, "SeenEvents", Seen)
    monkeypatch.setattr(lark_stream, "stream_events", events)

    async def main() -> None:
        def admit(event: dict[str, Any]) -> bool:
            return event["sender_id"] == OPEN_ID

        transport = BotTransport("cli_example", "example-secret", base_url="https://platform.invalid", admit=admit)
        assert transport.available()
        assert await transport.send(user_id=OPEN_ID, markdown="hello", idempotency_key="send-1", return_id=True) == "om_sent"
        assert await transport.reply("om_parent", text="reply", idempotency_key="reply-1", return_id=True) == "om_sent"
        await transport.edit("om_edit", text="updated")
        await transport.react("om_in")
        assert await transport.names(iter([OPEN_ID])) == {OPEN_ID: "Ada Lovelace"}
        for _ in range(2):
            assert [event async for event in transport.events()] == [_event()]
        assert made == [("cli_example", "example-secret", "https://platform.invalid")]
        assert transport_calls == [
            ("send", {"user_id": OPEN_ID, "markdown": "hello", "idempotency_key": "send-1"}),
            ("reply", {"message_id": "om_parent", "text": "reply", "idempotency_key": "reply-1"}),
            ("edit", {"message_id": "om_edit", "text": "updated"}),
            ("react", {"message_id": "om_in"}),
            ("names", {"open_ids": [OPEN_ID]}),
        ]
        (api, seen, policy, stats), (next_api, next_seen, next_policy, next_stats) = connections
        assert isinstance(seen, Seen) and seen is next_seen and api is next_api
        assert policy is next_policy is admit
        assert isinstance(stats, lark_stream.StreamStats) and stats is next_stats and stats.dropped == 0
        await transport.aclose()
        assert api.closed
        other = BotTransport("cli_other", "other-secret", base_url="https://platform.invalid")
        assert [event async for event in other.events()] == [_event()]
        assert connections[-1][1] is not seen
        assert connections[-1][2] is None and connections[-1][3] is not stats
        await other.aclose()

    asyncio.run(main())


@pytest.mark.parametrize(("domain", "base_url", "expected"), [
    ("feishu", None, "https://open.feishu.cn"),
    ("lark", None, "https://open.larksuite.com"),
    ("lark", "https://platform.invalid", "https://platform.invalid"),
])
def test_make_adapter_selects_the_domain_and_keeps_the_bot_channel_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, domain: str, base_url: str | None, expected: str,
) -> None:
    from looper.agents.channels import lark_api, lark_bot

    made: list[tuple[str, str, str]] = []

    class Api(StubTransport):
        def __init__(self, app_id: str, app_secret: str, *, base_url: str) -> None:
            super().__init__()
            made.append((app_id, app_secret, base_url))

    monkeypatch.setattr(lark_api, "LarkApi", Api)

    async def main() -> None:
        async def rejected(sender_id: str, chat_id: str | None) -> None:
            pass

        adapter = lark_bot.make_adapter({"app_id": " cli_example ", "domain": domain}, "example-secret",
                                        runtime_root=tmp_path,
                                        allowlist=SenderAllowlist("channel-lark_bot", frozenset({OPEN_ID})),
                                        on_rejected=rejected, bot_open_id=BOT_ID, base_url=base_url)
        assert adapter.channel_type == "lark_bot" and adapter.channel_id == "channel-lark_bot"
        assert adapter.healthcheck()["channel"] == "lark_bot"
        assert made == [("cli_example", "example-secret", expected)]
        assert "example-secret" not in str(adapter.healthcheck())
        assert adapter._backoff is True
        assert adapter._transport._admit({"sender_id": OPEN_ID}) is True
        assert adapter._transport._admit({"sender_id": OTHER_ID}) is False
        await adapter.stop()

    asyncio.run(main())


@pytest.mark.parametrize(("allowlist", "problem", "starts"), [
    ([OPEN_ID], None, True),
    ([OPEN_ID], "the platform rejected the app credentials: invalid credentials", False),
    ([], None, False),
    (["not-an-open-id"], None, False),
])
def test_serve_checks_bot_credentials_and_allowlist_before_building_the_adapter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
    allowlist: list[str], problem: str | None, starts: bool,
) -> None:
    from types import SimpleNamespace

    from looper.agents import serve
    from looper.agents.channels import lark_bot, lark_bot_settings
    from looper.agents.channels.lark_api import BotInfo
    from looper.db import channel_configs

    config = {"app_id": "cli_example", "domain": "feishu", "allowed_open_ids": allowlist}
    checked: list[tuple[Any, str, bool]] = []
    built: list[dict[str, Any]] = []
    rejected_rows: list[tuple[str, str, str | None]] = []
    sentinel = SimpleNamespace(channel_type="lark_bot", channel_id="channel-lark_bot")

    async def check(row: dict[str, Any], app_secret: str, *, fresh: bool = False) -> tuple[Any, Any]:
        checked.append((row, app_secret, fresh))
        return problem, None if problem else BotInfo("HelperBot", BOT_ID)

    def make_adapter(row: dict[str, Any], app_secret: str, **kwargs: Any) -> Any:
        built.append({"config": row, "secret": app_secret, **kwargs})
        return sentinel

    async def record_rejected(channel_id: str, sender_id: str, *, chat_id: str | None = None) -> None:
        rejected_rows.append((channel_id, sender_id, chat_id))

    monkeypatch.setattr(lark_bot_settings, "check", check)
    monkeypatch.setattr(lark_bot, "make_adapter", make_adapter)
    monkeypatch.setattr(channel_configs, "record_rejected", record_rejected)

    async def main() -> None:
        result = await serve._lark_bot_adapter(config, "example-secret", tmp_path)
        assert (result is sentinel) is starts
        if starts:
            assert checked == [(config, "example-secret", True)]
            (call,) = built
            assert call["config"] == config and call["secret"] == "example-secret"
            assert call["runtime_root"] == tmp_path and call["bot_open_id"] == BOT_ID
            assert call["allowlist"].ids == frozenset({OPEN_ID})
            assert call["allowlist"].channel_id == "channel-lark_bot"
            await call["on_rejected"](OTHER_ID, "oc_team")
            assert rejected_rows == [("channel-lark_bot", OTHER_ID, "oc_team")]
        else:
            assert built == []
            assert checked == ([(config, "example-secret", True)] if allowlist == [OPEN_ID] else [])
            assert "channel-lark_bot not started" in caplog.text

    with caplog.at_level("INFO"):
        asyncio.run(main())
    assert "example-secret" not in caplog.text
    if starts:
        assert "HelperBot" in caplog.text and "cli_example" in caplog.text
