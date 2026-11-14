"""Bot mentions use the same admission, receipts and reply path as live people."""

from __future__ import annotations

import asyncio
import importlib
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import pytest

from looper.agents.bus import MessageBus
from looper.agents.channels.allowlist import SenderAllowlist, sender_ids
from looper.agents.channels.chat_routing import RouteAction, RouteDecision, SenderPolicy
from looper.agents.channels.lark import LarkChannelAdapter
from looper.agents.channels.lark_inbox import LarkInbox, history_event
from looper.agents.message import MessageEnvelope, MessageType, channel_address, manager_address
from test_lark_transport import BOT_ID, BOT_MENTION, OPEN_ID, StubTransport, _event

OTHER_BOT = "ou_syntheticbot12"
OTHER_APP = "cli_syntheticbot12"
OWN_APP = "cli_syntheticself12"
CHAT = "oc_syntheticgroup"


class BotHistoryTransport(StubTransport):
    def __init__(self) -> None:
        super().__init__()
        self.pages: dict[str, dict[str, Any] | Exception] = {}
        self.list_calls: list[dict[str, Any]] = []

    async def list_messages(self, chat_id: str, *, start: float, end: float,
                            page_token: str = "") -> dict[str, Any]:
        self.list_calls.append({"chat_id": chat_id, "start": start, "end": end, "page_token": page_token})
        page = self.pages.get(chat_id, _page())
        if isinstance(page, Exception):
            raise page
        return page

    async def reply(self, message_id: str, *, reply_in_thread: bool = False,
                    return_id: bool = False, **kwargs: Any) -> str | None:
        return await self._call("reply", message_id=message_id,
                                **({"reply_in_thread": True} if reply_in_thread else {}),
                                **({"return_id": True} if return_id else {}), **kwargs)


def _page(*messages: dict[str, Any], chat_type: str = "group") -> dict[str, Any]:
    return {"chat_type": chat_type, "messages": list(messages), "has_more": False}


def _message(message_id: str, *, sender: str = OTHER_BOT, app: str = OTHER_APP,
             sender_type: str = "app", mentioned: bool = True, **fields: Any) -> dict[str, Any]:
    return {"message_id": message_id, "msg_type": "text",
            "sender": {"sender_type": sender_type, "id": app, "open_bot_id": sender},
            "mentions": [BOT_MENTION] if mentioned else [],
            "content": "@HelperBot review this" if mentioned else "review this",
            "create_time": str(int(time.time() * 1000)), **fields}


@pytest.fixture(autouse=True)
def isolate_platform(monkeypatch: pytest.MonkeyPatch) -> None:
    importlib.import_module("looper.agents.tiers.manager")

    async def observe(*args: Any, **kwargs: Any) -> None:
        pass

    async def bound_chats(self: LarkChannelAdapter) -> list[str]:
        return [CHAT]

    monkeypatch.setattr("looper.agents.channels.lark.observe_chat", observe)
    monkeypatch.setattr(LarkChannelAdapter, "_bound_chats", bound_chats, raising=False)


@asynccontextmanager
async def _channel(root: Path, *, allowed: frozenset[str] = frozenset({OPEN_ID, OTHER_BOT}),
                   **kwargs: Any):
    transport = BotHistoryTransport()
    instance = LarkChannelAdapter(
        runtime_root=root, transport=transport, bot_open_id=BOT_ID, bot_app_id=OWN_APP,
        allowlist=SenderAllowlist("channel-lark", allowed),
        manager_running=kwargs.pop("manager_running", lambda _: True),
        resolve_chat=kwargs.pop("resolve_chat", lambda *_: RouteDecision(RouteAction.ADMIT, "manager-helper", True)),
        **kwargs,
    )
    bus = MessageBus(runtime_root=root)
    instance._bus = bus
    instance._inbox = await LarkInbox.open(root / "inbox.sqlite3")
    try:
        yield instance, transport, bus
    finally:
        await instance.stop()
        await bus.shutdown()


@pytest.mark.parametrize("binding_only", [False, True])
def test_allowed_bot_poll_overlap_handles_once_and_replies_to_its_thread_message(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, binding_only: bool,
) -> None:
    async def run() -> None:
        clock = [time.time()]
        monkeypatch.setattr("looper.agents.channels.lark.time", lambda: clock[0])
        async with _channel(tmp_path,
                allowed=frozenset({OPEN_ID}) if binding_only else frozenset({OPEN_ID, OTHER_BOT}),
                resolve_chat=lambda *_: RouteDecision(RouteAction.ADMIT, "manager-helper", True,
                    senders=frozenset({OTHER_BOT}) if binding_only else frozenset())) as (instance, transport, bus):
            transport.pages[CHAT] = _page(_message(
                "om_syntheticthreadreply", thread_id="omt_syntheticthread",
                parent_id="om_syntheticparent", root_id="om_syntheticroot",
                create_time=str(int(clock[0] * 1000))))
            last_poll: dict[str, float] = {}
            with caplog.at_level("INFO"):
                await instance._poll_bot_chats(last_poll)
                clock[0] += 30
                await instance._poll_bot_chats(last_poll)
                assert await instance._handle_lark_event(history_event(
                    transport.pages[CHAT]["messages"][0], CHAT, "group")) == "duplicate"
            (turn,) = bus.journal.undelivered()
            assert turn.target == manager_address("manager-helper")
            assert turn.payload["text"] == "review this"
            context = turn.payload["channel_context"]
            assert context["sender_id"] == OTHER_BOT
            assert context["reply_to"] == "om_syntheticparent"
            assert context["root_id"] == "om_syntheticroot"
            assert context["thread_id"] == "omt_syntheticthread"
            assert (await instance._inbox.processed("om_syntheticthreadreply"))
            receipt = await instance.deliver(MessageEnvelope.create(
                source=turn.target, target=channel_address("lark"), message_type=MessageType.AGENT_REPLY,
                correlation_id=turn.correlation_id, payload={"text": "review received"}))
            assert receipt.success
            assert ("reply", {"message_id": "om_syntheticthreadreply", "markdown": "review received",
                              "reply_in_thread": True}) in transport.calls
            assert len(transport.list_calls) == 2
            assert transport.list_calls[1]["start"] < transport.list_calls[0]["end"]
            handled = [record.message for record in caplog.records
                       if "outcome=handled" in record.message and "om_syntheticthreadreply" in record.message]
            assert len(handled) == 1
            assert CHAT in handled[0] and OTHER_BOT in handled[0]

    asyncio.run(asyncio.wait_for(run(), 5))


@pytest.mark.parametrize("slow_stage", ["history", "thread"])
def test_slow_bot_poll_keeps_each_chats_queried_watermark(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, slow_stage: str,
) -> None:
    async def run() -> None:
        now = time.time()
        clock = [now]
        chats = [CHAT, "oc_syntheticsecond"]
        monkeypatch.setattr("looper.agents.channels.lark.time", lambda: clock[0])

        async def bound(self: LarkChannelAdapter) -> list[str]:
            return chats

        monkeypatch.setattr(LarkChannelAdapter, "_bound_chats", bound)
        async with _channel(tmp_path) as (instance, transport, bus):
            for chat in chats:
                await instance._inbox.remember_bot(chat, now - 86400)
                await instance._inbox.remember_thread(chat, f"omt_{chat}", now - 1)
            messages = {chat: _message(f"om_syntheticduring{index}",
                                      create_time=str(int((now + 5 + index * 10) * 1000)))
                        for index, chat in enumerate(chats)}
            original_list = transport.list_messages

            async def history(chat_id: str, **kwargs: Any) -> dict[str, Any]:
                await original_list(chat_id, **kwargs)
                if slow_stage == "history":
                    clock[0] += 60
                    message = messages[chat_id]
                    return _page(*([message] if kwargs["start"] <= float(message["create_time"]) / 1000
                                   <= kwargs["end"] else []))
                return _page()

            async def roots(chat_id: str, *, page_token: str = "") -> dict[str, Any]:
                return {"roots": [{"thread_id": f"omt_{chat_id}", "create_time": str(int(now * 1000))}],
                        "has_more": False}

            async def replies(thread_id: str, *, page_token: str = "") -> dict[str, Any]:
                clock[0] += 60
                return _page(messages[thread_id.removeprefix("omt_")])

            transport.list_messages = history
            if slow_stage == "thread":
                transport.list_threads, transport.list_thread_messages = roots, replies
            last_poll: dict[str, float] = {}
            await instance._poll_bot_chats(last_poll)
            (turn,) = bus.journal.undelivered()
            assert turn.payload["channel_context"]["message_id"] == messages[chats[1]]["message_id"]
            assert last_poll == {CHAT: now, chats[1]: now + 60}
            clock[0] += 30
            await instance._poll_bot_chats(last_poll)
            assert {turn.payload["channel_context"]["message_id"] for turn in bus.journal.undelivered()} == {
                message["message_id"] for message in messages.values()}
            for first, second in zip(transport.list_calls[:2], transport.list_calls[2:]):
                assert second["start"] == first["end"] - 10
                assert last_poll[second["chat_id"]] == second["end"]
            clock[0] += 30
            await instance._poll_bot_chats(last_poll)
            assert len(bus.journal.undelivered()) == 2

    asyncio.run(asyncio.wait_for(run(), 5))


@pytest.mark.parametrize("watermark_age,lookback", [(120, 130), (1800, 900)])
def test_bot_poll_recovers_from_watermark_with_only_max_age_cap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, watermark_age: int, lookback: int,
) -> None:
    async def run() -> None:
        now = time.time()
        monkeypatch.setattr("looper.agents.channels.lark.time", lambda: now)
        async with _channel(tmp_path) as (instance, transport, bus):
            await instance._inbox.remember_bot(CHAT, now - 86400)
            transport.pages[CHAT] = _page(_message(
                "om_syntheticdelayed", create_time=str(int((now - lookback + 1) * 1000))))
            last_poll = {CHAT: now - watermark_age}
            await instance._poll_bot_chats(last_poll)
            assert len(bus.journal.undelivered()) == 1
            assert transport.list_calls[0]["start"] == now - lookback
            assert last_poll[CHAT] == transport.list_calls[0]["end"] == now

    asyncio.run(asyncio.wait_for(run(), 5))


def test_unlisted_bot_is_recorded_but_warns_once_per_chat_per_hour(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    async def run() -> None:
        rejected = []
        clock = [time.monotonic()]
        monkeypatch.setattr("looper.agents.channels.lark.monotonic", lambda: clock[0])

        async def reject(sender: str, chat: str | None) -> None:
            rejected.append((sender, chat))

        async with _channel(tmp_path, allowed=frozenset({OPEN_ID}), on_rejected=reject) as (instance, transport, bus):
            with caplog.at_level("WARNING"):
                transport.pages[CHAT] = _page(_message("om_syntheticrefusedfirst"))
                await instance._poll_bot_chats({})
                transport.pages[CHAT] = _page(_message("om_syntheticrefusedsecond"))
                await instance._poll_bot_chats({})
                warnings = [record for record in caplog.records if "not on the sender allowlist" in record.message]
                assert len(warnings) == 1
                clock[0] += 3601
                transport.pages[CHAT] = _page(_message("om_syntheticrefusedlater"))
                await instance._poll_bot_chats({})
            assert bus.journal.undelivered() == []
            assert rejected == [(OTHER_BOT, CHAT)] * 3
            assert all([(await instance._inbox.processed(message)) for message in (
                "om_syntheticrefusedfirst", "om_syntheticrefusedsecond", "om_syntheticrefusedlater")])
            assert len([record for record in caplog.records if "not on the sender allowlist" in record.message]) == 2
            assert not any(kind in {"send", "reply", "react"} for kind, _ in transport.calls)

    asyncio.run(asyncio.wait_for(run(), 5))


@pytest.mark.parametrize("fields", [
    {"sender": BOT_ID}, {"sender": "", "app": OWN_APP}, {"mentioned": False},
])
def test_polls_ignore_own_bot_and_messages_without_our_mention(tmp_path: Path, fields: dict[str, Any]) -> None:
    async def run() -> None:
        async with _channel(tmp_path, allowed=frozenset({OPEN_ID, OTHER_BOT, BOT_ID, OWN_APP})) as (instance, transport, bus):
            transport.pages[CHAT] = _page(_message("om_syntheticignored", **fields))
            await instance._poll_bot_chats({})
            assert bus.journal.undelivered() == []
            assert not any(kind in {"send", "reply", "react"} for kind, _ in transport.calls)

    asyncio.run(asyncio.wait_for(run(), 5))


@pytest.mark.parametrize("sender_id,sender_app_id", [(BOT_ID, ""), ("ou_syntheticother", OWN_APP)])
def test_live_own_bot_message_is_ignored_before_chat_admission(
    tmp_path: Path, sender_id: str, sender_app_id: str,
) -> None:
    async def run() -> None:
        async with _channel(tmp_path, resolve_chat=lambda *_: RouteDecision(
                RouteAction.ADMIT, "manager-helper", True, sender_policy=SenderPolicy.CHAT)) as (instance, transport, bus):
            await instance._handle_lark_event(_event(
                chat_id=CHAT, chat_type="group", message_id="om_syntheticself", mentions=[BOT_MENTION],
                sender_type="bot", sender_id=sender_id, sender_app_id=sender_app_id))
            assert bus.journal.undelivered() == []
            assert not any(kind in {"send", "reply", "react"} for kind, _ in transport.calls)

    asyncio.run(asyncio.wait_for(run(), 5))


def test_eleventh_bot_turn_is_silent_and_rolling_hour_expires(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    async def run() -> None:
        clock = [time.monotonic()]
        monkeypatch.setattr("looper.agents.channels.lark.monotonic", lambda: clock[0])
        async with _channel(tmp_path) as (instance, transport, bus):
            transport.pages[CHAT] = _page(*[_message(f"om_syntheticturn{index}") for index in range(11)])
            with caplog.at_level("WARNING"):
                await instance._poll_bot_chats({})
            assert len(bus.journal.undelivered()) == 10
            warnings = [record.message for record in caplog.records if record.levelname == "WARNING"]
            assert any(CHAT in message and "count=11" in message for message in warnings)
            assert not any(kind in {"reply", "send"} for kind, _ in transport.calls)
            # The exhausted budget also suppresses the disabled-Manager reply.
            instance._manager_running = lambda _: False
            await instance._handle_lark_event(history_event(_message("om_syntheticdisabledlimit"), CHAT, "group"))
            assert not any(kind in {"reply", "send"} for kind, _ in transport.calls)
            instance._manager_running = lambda _: True
            clock[0] += 3601
            transport.pages[CHAT] = _page(_message("om_syntheticnextwindow"))
            await instance._poll_bot_chats({})
            assert len(bus.journal.undelivered()) == 11

    asyncio.run(asyncio.wait_for(run(), 5))


def test_people_stay_on_live_consumer_and_share_receipts_with_history(tmp_path: Path) -> None:
    async def run() -> None:
        async with _channel(tmp_path) as (instance, transport, bus):
            person = _message("om_syntheticperson", sender_type="user", app=OPEN_ID, sender="")
            event = history_event(person, CHAT, "group")
            transport.pages[CHAT] = _page(person)
            await instance._poll_bot_chats({})
            assert bus.journal.undelivered() == []
            assert await instance._handle_lark_event(event) == "handled"
            await instance._poll_bot_chats({})
            assert await instance._handle_lark_event(event) == "duplicate"
            assert len(bus.journal.undelivered()) == 1

    asyncio.run(asyncio.wait_for(run(), 5))


def test_chat_policy_admits_unlisted_people_and_bots_only_in_that_group(
    tmp_path: Path, caplog: pytest.LogCaptureFixture,
) -> None:
    async def run() -> None:
        def route(channel: str, chat: str) -> RouteDecision:
            return RouteDecision(RouteAction.ADMIT, "manager-helper", chat == CHAT,
                                 sender_policy=SenderPolicy.CHAT if chat == CHAT else SenderPolicy.LISTED)

        async with _channel(tmp_path, allowed=frozenset({OPEN_ID}), resolve_chat=route) as (instance, _, bus):
            with caplog.at_level("INFO"):
                for chat in (CHAT, "oc_syntheticother"):
                    for kind in ("user", "app"):
                        await instance._handle_lark_event(_event(
                            chat_id=chat, chat_type="group", message_id=f"om_synthetic{chat}{kind}",
                            mentions=[BOT_MENTION], sender_type=kind, sender_id=OTHER_BOT))
                await instance._handle_lark_event(_event(
                    message_id="om_syntheticdm", sender_id=OTHER_BOT, chat_id=CHAT))
            turns = bus.journal.undelivered()
            assert len(turns) == 2
            assert all(turn.payload["channel_context"]["chat_id"] == CHAT for turn in turns)
            assert len([record for record in caplog.records if "chat_route " in record.message
                        and "sender_policy=chat" in record.message]) == 2

    asyncio.run(asyncio.wait_for(run(), 5))


def test_poll_failure_keeps_other_bound_chats_running(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    async def run() -> None:
        async def bound(self: LarkChannelAdapter) -> list[str]:
            return ["oc_syntheticfailure", CHAT]

        monkeypatch.setattr(LarkChannelAdapter, "_bound_chats", bound)
        async with _channel(tmp_path) as (instance, transport, bus):
            transport.pages["oc_syntheticfailure"] = ConnectionError("stand-in unavailable")
            transport.pages[CHAT] = _page(_message("om_synthetichealthy"))
            with caplog.at_level("WARNING"):
                await instance._poll_bot_chats({})
            assert len(bus.journal.undelivered()) == 1
            assert any(record.levelname == "WARNING" and "oc_syntheticfailure" in record.message
                       for record in caplog.records)

    asyncio.run(asyncio.wait_for(run(), 5))


def test_history_converter_prefers_bot_open_id_and_preserves_app_fallback() -> None:
    event = history_event(_message("om_syntheticconverted"), CHAT, "group")
    assert event["sender_id"] == OTHER_BOT
    assert event["sender_app_id"] == OTHER_APP
    fallback = history_event(_message("om_syntheticfallback", sender=""), CHAT, "group")
    assert fallback["sender_id"] == OTHER_APP


@pytest.mark.parametrize("later_page_fails", [False, True])
def test_bot_poll_combines_thread_discovery_activity_and_partial_page_recovery(
    tmp_path: Path, later_page_fails: bool,
) -> None:
    async def run() -> None:
        async with _channel(tmp_path) as (instance, transport, bus):
            thread = "omt_syntheticlisted"
            active = "omt_syntheticactive"
            root = _message("om_syntheticroot", sender_type="user", sender=OPEN_ID, thread_id=thread)
            transport.pages[CHAT] = _page(root)
            await instance._inbox.remember_thread(CHAT, active, time.time() - 3 * 86400)
            reads: list[tuple[str, str]] = []

            async def roots(chat_id: str, *, page_token: str = "") -> dict[str, Any]:
                return {"roots": [root], "has_more": False}

            async def replies(thread_id: str, *, page_token: str = "") -> dict[str, Any]:
                reads.append((thread_id, page_token))
                if page_token:
                    raise ConnectionError("stand-in later page unavailable")
                return {**_page(
                    _message(f"om_syntheticown{thread_id}", sender=BOT_ID, create_time=root["create_time"]),
                    _message(f"om_syntheticperson{thread_id}", sender_type="user", sender=OPEN_ID,
                             create_time=root["create_time"]),
                    _message(f"om_syntheticbot{thread_id}", create_time=root["create_time"]),
                ), "has_more": later_page_fails, "page_token": "next"}

            transport.list_threads, transport.list_thread_messages = roots, replies
            last_poll: dict[str, float] = {}
            await instance._poll_bot_chats(last_poll)
            turns = bus.journal.undelivered()
            assert {turn.payload["channel_context"]["message_id"] for turn in turns} == {
                f"om_syntheticbot{thread}", f"om_syntheticbot{active}"}
            assert {turn.payload["channel_context"]["thread_id"] for turn in turns} == {thread, active}
            assert reads.count((thread, "")) == reads.count((active, "")) == 1
            assert (CHAT in last_poll) is not later_page_fails

    asyncio.run(asyncio.wait_for(run(), 5))


def test_app_id_fallback_can_be_allowlisted(tmp_path: Path) -> None:
    async def run() -> None:
        async with _channel(tmp_path, allowed=frozenset(sender_ids("lark", [OPEN_ID, OTHER_APP]))) as (instance, transport, bus):
            transport.pages[CHAT] = _page(_message("om_syntheticfallback", sender=""))
            await instance._poll_bot_chats({})
            (turn,) = bus.journal.undelivered()
            assert turn.payload["channel_context"]["sender_id"] == OTHER_APP

    asyncio.run(asyncio.wait_for(run(), 5))


def test_polled_bot_attachment_failure_keeps_payload_and_replies_to_original_message(tmp_path: Path) -> None:
    async def run() -> None:
        async with _channel(tmp_path) as (instance, transport, bus):
            async def download(*args: Any, **kwargs: Any) -> None:
                raise OSError("stand-in cannot save image")

            transport.download = download
            transport.pages[CHAT] = _page(_message(
                "om_syntheticattachment", msg_type="post",
                content="@HelperBot review this ![Image](img_synthetic)", thread_id="omt_syntheticattachment"))
            await instance._poll_bot_chats({})
            (turn,) = bus.journal.undelivered()
            (attachment,) = turn.payload["attachments"]
            assert attachment["state"] == "failed"
            assert attachment["told"]
            replies = [payload for kind, payload in transport.calls if kind == "reply"]
            assert len(replies) == 1
            assert replies[0]["message_id"] == "om_syntheticattachment"
            assert replies[0]["text"].startswith("有 1 个附件没有收下：")

    asyncio.run(asyncio.wait_for(run(), 5))


def test_polled_bot_obeys_disabled_manager_route(tmp_path: Path) -> None:
    from looper.agents.tiers.manager import TURNED_OFF_REPLY

    async def run() -> None:
        async with _channel(tmp_path, manager_running=lambda _: False) as (instance, transport, bus):
            transport.pages[CHAT] = _page(_message("om_syntheticdisabled"))
            await instance._poll_bot_chats({})
            assert bus.journal.undelivered() == []
            assert transport.calls == [("reply", {"message_id": "om_syntheticdisabled", "text": TURNED_OFF_REPLY})]

    asyncio.run(asyncio.wait_for(run(), 5))


@pytest.mark.parametrize("policy,bot_age,interval", [
    (SenderPolicy.CHAT, None, 30),
    (SenderPolicy.LISTED, 86400, 30),
    (SenderPolicy.LISTED, None, 300),
    (SenderPolicy.LISTED, 7 * 86400 + 1, 300),
])
def test_bound_chat_bot_poll_cadence(tmp_path, monkeypatch, policy, bot_age, interval):
    async def run():
        clock = [time.time()]
        monkeypatch.setattr("looper.agents.channels.lark.time", lambda: clock[0])
        async with _channel(tmp_path, resolve_chat=lambda *_: RouteDecision(
                RouteAction.ADMIT, "manager-helper", True, sender_policy=policy)) as (instance, transport, _):
            await instance._inbox.remember_bot("unrelated-chat", clock[0])
            if bot_age is not None:
                await instance._inbox.remember_bot(CHAT, clock[0] - bot_age)
                # Cadence evidence must survive a channel restart.
                await instance._inbox.close()
                instance._inbox = await LarkInbox.open(tmp_path / "inbox.sqlite3")
            last_poll = {}
            await instance._poll_bot_chats(last_poll)
            assert len(transport.list_calls) == 1
            assert transport.list_calls[0]["start"] == clock[0] - interval - 10
            clock[0] += interval - 1
            await instance._poll_bot_chats(last_poll)
            assert len(transport.list_calls) == 1
            clock[0] += 1
            await instance._poll_bot_chats(last_poll)
            assert len(transport.list_calls) == 2
            assert transport.list_calls[1]["start"] == transport.list_calls[0]["end"] - 10

    asyncio.run(asyncio.wait_for(run(), 5))


@pytest.mark.parametrize("live,age", [(False, 0), (False, 86400), (True, 0)])
def test_bot_observation_promotes_slow_chat_even_without_mention(tmp_path, monkeypatch, live, age):
    async def run():
        clock = [time.time()]
        monkeypatch.setattr("looper.agents.channels.lark.time", lambda: clock[0])
        async with _channel(tmp_path) as (instance, transport, bus):
            last_poll = {}
            await instance._poll_bot_chats(last_poll)
            clock[0] += 30
            await instance._poll_bot_chats(last_poll)
            assert len(transport.list_calls) == 1
            posted_at = clock[0] - age
            message = _message("om_syntheticactivity", mentioned=False,
                               create_time=str(int(posted_at * 1000)))
            if live:
                await instance._handle_lark_event(history_event(message, CHAT, "group"))
            else:
                clock[0] += 270
                transport.pages[CHAT] = _page(message)
                await instance._poll_bot_chats(last_poll)
            assert bus.journal.undelivered() == []
            calls = len(transport.list_calls)
            clock[0] += 30
            await instance._poll_bot_chats(last_poll)
            assert len(transport.list_calls) == calls + 1
            # Replaying an old history page cannot keep a chat fast indefinitely.
            transport.pages[CHAT] = _page()
            clock[0] = posted_at + 7 * 86400 + 1
            await instance._poll_bot_chats(last_poll)
            calls = len(transport.list_calls)
            clock[0] += 30
            await instance._poll_bot_chats(last_poll)
            assert len(transport.list_calls) == calls

    asyncio.run(asyncio.wait_for(run(), 5))


@pytest.mark.parametrize("thread_age,scan", [(None, False), (86401, False), (86399, True)])
def test_quiet_bot_poll_skips_thread_discovery(tmp_path, thread_age, scan):
    async def run():
        async with _channel(tmp_path) as (instance, transport, _):
            if thread_age is not None:
                await instance._inbox.remember_thread(CHAT, "omt_syntheticcandidate", time.time() - thread_age)
            calls = []

            async def roots(chat_id, *, page_token=""):
                calls.append("roots")
                return {"roots": [], "has_more": False}

            async def replies(thread_id, *, page_token=""):
                calls.append("replies")
                return _page()

            transport.list_threads, transport.list_thread_messages = roots, replies
            await instance._poll_bot_chats({})
            assert len(transport.list_calls) == 1
            assert calls == (["roots", "replies"] if scan else [])

    asyncio.run(asyncio.wait_for(run(), 5))


def test_bot_poll_logs_cadence_counts_hourly(tmp_path, monkeypatch, caplog):
    async def run():
        clock = [time.time()]
        monkeypatch.setattr("looper.agents.channels.lark.time", lambda: clock[0])

        async def bound(self):
            return [CHAT, "slow-chat"]

        monkeypatch.setattr(LarkChannelAdapter, "_bound_chats", bound)
        async with _channel(tmp_path, resolve_chat=lambda _, chat: RouteDecision(
                RouteAction.ADMIT, "manager-helper", True,
                sender_policy=SenderPolicy.CHAT if chat == CHAT else SenderPolicy.LISTED)) as (instance, _, _):
            with caplog.at_level("INFO"):
                last_poll = {}
                for elapsed in (0, 30, 3599, 3600):
                    # Advance wall time without sleeping or starting CLI processes.
                    monkeypatch.setattr("looper.agents.channels.lark.monotonic", lambda: elapsed)
                    await instance._poll_bot_chats(last_poll)
            summaries = [r.message for r in caplog.records if "lark_bot_poll_cadence " in r.message]
            assert len(summaries) == 2
            assert all("fast=1 slow=1" in summary for summary in summaries)

    asyncio.run(asyncio.wait_for(run(), 5))
