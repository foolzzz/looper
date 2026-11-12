"""Restart recovery uses the normal Lark ingress with an in-memory history transport."""

from __future__ import annotations

import asyncio
import importlib
import time
from collections.abc import AsyncIterator, Iterable
from pathlib import Path
from typing import Any

import pytest

from looper.agents.bus import MessageBus
from looper.agents.channels.allowlist import SenderAllowlist
from looper.agents.channels.chat_routing import RouteAction, RouteDecision
from looper.agents.channels.lark import LarkChannelAdapter
from looper.agents.message import MessageType
from test_lark_transport import BOT_ID, BOT_MENTION, OPEN_ID, StubTransport, _event


class HistoryTransport(StubTransport):
    """Keep live ingress open while tests release a deterministic history page."""

    def __init__(self) -> None:
        super().__init__()
        self.incoming: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self.consumer_started = asyncio.Event()
        self.live_received, self.live_handled = asyncio.Event(), asyncio.Event()
        self.before_names: asyncio.Event | None = None
        self.release_names: asyncio.Event | None = None
        self.list_calls: list[dict[str, Any]] = []
        self.pages: dict[str, list[dict[str, Any] | Exception]] = {}
        self.before_list: asyncio.Event | None = None
        self.release_list: asyncio.Event | None = None

    async def events(self) -> AsyncIterator[dict[str, Any]]:
        self.consumer_started.set()
        try:
            while True:
                event = await self.incoming.get()
                self.live_received.set()
                yield event
                self.live_handled.set()
        finally:
            self.stream_closed.set()

    async def names(self, open_ids: Iterable[str]) -> dict[str, str]:
        if self.before_names is not None:
            self.before_names.set()
        if self.release_names is not None:
            await self.release_names.wait()
        return await super().names(open_ids)

    async def list_messages(
        self, chat_id: str, *, start: float, end: float, page_token: str = "",
    ) -> dict[str, Any]:
        assert self.consumer_started.is_set(), "history must wait for the live consumer"
        self.list_calls.append({"chat_id": chat_id, "start": start, "end": end, "page_token": page_token})
        if self.before_list is not None:
            self.before_list.set()
        if self.release_list is not None:
            await self.release_list.wait()
        result = self.pages[chat_id].pop(0)
        if isinstance(result, Exception):
            raise result
        return result


def adapter(root: Path, transport: HistoryTransport, **kwargs: Any) -> LarkChannelAdapter:
    return LarkChannelAdapter(
        runtime_root=root, transport=transport,
        allowlist=SenderAllowlist("channel-lark", frozenset({OPEN_ID})),
        bot_open_id=BOT_ID, manager_running=lambda _: True,
        resolve_chat=lambda *_: RouteDecision(RouteAction.ADMIT, "manager-helper", True),
        **kwargs,
    )


def history_message(message_id: str, *, at: float, mentioned: bool = True, **fields: Any) -> dict[str, Any]:
    return {
        "message_id": message_id, "msg_type": "text", "sender": {"id": OPEN_ID, "sender_type": "user"},
        "mentions": [BOT_MENTION] if mentioned else [],
        "content": "@HelperBot hello" if mentioned else "hello",
        "create_time": str(int(at * 1000)), **fields,
    }


def seed_boundary(root: Path, at: float) -> None:
    from looper.agents.channels.lark_inbox import LarkInbox

    inbox = LarkInbox(root / ".local" / "lark" / "channel-lark" / "inbox.sqlite3")
    try:
        inbox.checkpoint(at)
    finally:
        inbox.close()


async def catchup_done(instance: LarkChannelAdapter, transport: HistoryTransport) -> None:
    await asyncio.wait_for(transport.consumer_started.wait(), 3)
    assert instance._catchup_task is not None
    await asyncio.wait_for(instance._catchup_task, 3)


@pytest.fixture(autouse=True)
def isolate_observation(monkeypatch: pytest.MonkeyPatch) -> None:
    # serve loads the Manager before starting adapters. Keep that cold import
    # outside the scenarios' deadlock watchdog, as it is in the real service.
    importlib.import_module("looper.agents.tiers.manager")

    async def observe(*args: Any, **kwargs: Any) -> None:
        pass

    async def chats(self: LarkChannelAdapter) -> list[str]:
        return ["oc_synthetic_group"]

    monkeypatch.setattr("looper.agents.channels.lark.observe_chat", observe)
    monkeypatch.setattr(LarkChannelAdapter, "_catchup_chats", chats, raising=False)


def test_gap_group_mention_reaches_bound_manager_once_after_start(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    async def run() -> None:
        now = time.time()
        seed_boundary(tmp_path, now - 60)
        transport = HistoryTransport()
        transport.pages["oc_synthetic_group"] = [{"chat_type": "group", "messages": [
            history_message("om_synthetic_gap", at=now - 30, reply_to="om_synthetic_parent", root_id="om_synthetic_root")],
            "has_more": False}]
        instance = adapter(tmp_path, transport)
        bus = MessageBus(runtime_root=tmp_path)
        try:
            with caplog.at_level("INFO", logger="looper.agents.channels.lark"):
                await instance.start(bus)
                await catchup_done(instance, transport)
            (message,) = bus.journal.undelivered()
            assert message.target.agent_id == "manager-helper"
            assert message.payload["text"] == "hello"
            assert message.payload["channel_context"] == {
                "chat_id": "oc_synthetic_group", "chat_type": "group", "sender_id": OPEN_ID,
                "message_id": "om_synthetic_gap", "reply_to": "om_synthetic_parent", "root_id": "om_synthetic_root",
                "sender_name": "Ada Lovelace",
            }
            assert transport.list_calls[0]["start"] == pytest.approx(now - 70, abs=0.1)
            summaries = [record.message for record in caplog.records if "lark_catchup " in record.message]
            assert len(summaries) == 1
            assert all(value in summaries[0] for value in (
                "boundary=", "chats_scanned=1", "messages_found=1", "handled=1", "duplicates=0", "too_old=0", "failures=0"))
        finally:
            await instance.stop()
            await bus.shutdown()

    asyncio.run(asyncio.wait_for(run(), 5))


@pytest.mark.parametrize("mode", ["group", "topic"])
def test_restart_recovers_nested_and_old_thread_mentions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str,
) -> None:
    import json

    from looper.agents.channels import lark_cli
    from looper.agents.channels.lark_transport import CliTransport

    async def run() -> None:
        now = time.time()
        seed_boundary(tmp_path, now - 60)
        nested = history_message("om_nested", at=now - 40, parent_id="om_recent", root_id="om_recent")
        old_reply = history_message("om_old_reply", at=now - 30, parent_id="om_old", root_id="om_old")
        calls = []

        async def run_json(argv):
            calls.append(argv)
            if "/open-apis/im/v1/chats/oc_synthetic_group" in argv:
                return {"data": {"chat_mode": mode}}
            if "+chat-messages-list" in argv:
                return {"messages": [history_message("om_recent", at=now - 50, mentioned=False,
                                                    thread_replies=[nested])], "has_more": False}
            if "/open-apis/im/v1/messages" in argv:
                params = json.loads(argv[argv.index("--params") + 1])
                assert params["container_id_type"] == "chat" and params["container_id"] == "oc_synthetic_group"
                assert "start_time" not in params
                if not params.get("page_token"):
                    return {"data": {"items": [{"thread_id": "omt_recent"}],
                                     "has_more": True, "page_token": "roots-next"}}
                assert params["page_token"] == "roots-next"
                return {"data": {"items": [{"thread_id": "omt_old", "create_time": str(int((now - 86400) * 1000))}],
                                 "has_more": False}}
            assert "+threads-messages-list" in argv
            assert argv[argv.index("--order") + 1] == "desc"
            assert argv[argv.index("--page-size") + 1] == "50"
            assert "--page-all" not in argv and "--start" not in argv and "--end" not in argv
            assert "--no-reactions" in argv
            thread = argv[argv.index("--thread") + 1]
            if thread == "omt_recent":
                return {"messages": [nested], "has_more": False}
            assert thread == "omt_old"
            if "--page-token" not in argv:
                return {"messages": [
                    history_message("om_future", at=now + 60),
                    history_message("om_latest_reply", at=now - 5, parent_id="om_old", root_id="om_old"),
                    history_message("om_unmentioned", at=now - 10, mentioned=False),
                    history_message("om_denied", at=now - 20, sender={"id": "ou_denied", "sender_type": "user"}),
                ], "has_more": True, "page_token": "replies-next"}
            assert argv[argv.index("--page-token") + 1] == "replies-next"
            return {"messages": [old_reply, history_message("om_expired", at=now - 86400)],
                    "has_more": True, "page_token": "older-than-window"}

        monkeypatch.setattr(lark_cli, "run_json", run_json)
        transport = HistoryTransport()
        cli = CliTransport("synthetic-profile")
        transport.list_messages = cli.list_messages
        transport.list_threads = cli.list_threads
        transport.list_thread_messages = cli.list_thread_messages
        instance = adapter(tmp_path, transport)
        bus = MessageBus(runtime_root=tmp_path)
        try:
            await instance.start(bus)
            await catchup_done(instance, transport)
            messages = bus.journal.undelivered()
            assert [m.payload["channel_context"]["message_id"] for m in messages] == [
                "om_nested", "om_old_reply", "om_latest_reply"]
            assert all(m.target.agent_id == "manager-helper" and m.payload["text"] == "hello" for m in messages)
            assert messages[1].payload["channel_context"]["reply_to"] == "om_old"
            assert messages[1].payload["channel_context"]["root_id"] == "om_old"
            assert instance._inbox.processed("om_old_reply")
            assert not instance._inbox.processed("om_future")
            assert len([c for c in calls if "+threads-messages-list" in c]) == 3
            assert all(c[:3] == ["lark-cli", "--profile", "synthetic-profile"] and "bot" in c for c in calls)
        finally:
            await instance.stop()
            await bus.shutdown()

    asyncio.run(asyncio.wait_for(run(), 5))


@pytest.mark.parametrize("failure", ["root_error", "root_limit", "reply_error", "reply_limit", "reply_cursor"])
def test_incomplete_thread_scan_preserves_checkpoint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str,
) -> None:
    from looper.agents.channels.lark_cli import LarkCliError
    from looper.agents.channels.lark_inbox import LarkInbox

    monkeypatch.setattr("looper.agents.channels.lark._CATCHUP_MAX_PAGES", 1)
    monkeypatch.setattr("looper.agents.channels.lark._HEARTBEAT_SECONDS", 0.01)

    async def run() -> None:
        boundary = time.time() - 60
        seed_boundary(tmp_path, boundary)
        entered, release, saved = asyncio.Event(), asyncio.Event(), asyncio.Event()
        transport = HistoryTransport()
        transport.pages["oc_synthetic_group"] = [{"messages": [], "chat_type": "group", "has_more": False}]

        async def list_threads(chat_id, *, page_token=""):
            if failure.startswith("root"):
                entered.set()
                await release.wait()
                if failure == "root_error":
                    raise LarkCliError("synthetic root read failure")
                return {"threads": [], "has_more": True, "page_token": "next"}
            return {"threads": ["omt_old"], "has_more": False}

        async def list_thread_messages(thread_id, *, page_token=""):
            entered.set()
            await release.wait()
            if failure == "reply_error":
                raise LarkCliError("synthetic reply read failure")
            return {"messages": [], "chat_type": "group", "has_more": True,
                    "page_token": "" if failure == "reply_cursor" else "next"}

        transport.list_threads = list_threads
        transport.list_thread_messages = list_thread_messages
        instance = adapter(tmp_path, transport)
        bus = MessageBus(runtime_root=tmp_path)
        try:
            await instance.start(bus)
            checkpoint = instance._inbox.checkpoint

            def save(at):
                checkpoint(at)
                saved.set()

            monkeypatch.setattr(instance._inbox, "checkpoint", save)
            await asyncio.wait_for(entered.wait(), 3)
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(saved.wait(), 0.05)
            release.set()
            await catchup_done(instance, transport)
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(saved.wait(), 0.05)
            assert instance._inbox.boundary == boundary
        finally:
            await instance.stop()
            await bus.shutdown()
        inbox = LarkInbox(tmp_path / ".local" / "lark" / "channel-lark" / "inbox.sqlite3")
        try:
            assert inbox.boundary == boundary, "clean stop must also preserve incomplete thread recovery"
        finally:
            inbox.close()

    asyncio.run(asyncio.wait_for(run(), 5))


@pytest.mark.parametrize("live_first", [True, False])
def test_live_event_racing_history_is_published_once(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, live_first: bool,
) -> None:
    async def run() -> None:
        now = time.time()
        seed_boundary(tmp_path, now - 60)
        transport = HistoryTransport()
        if live_first:
            transport.before_list, transport.release_list = asyncio.Event(), asyncio.Event()
        else:
            transport.before_names, transport.release_names = asyncio.Event(), asyncio.Event()
        transport.pages["oc_synthetic_group"] = [{"chat_type": "group", "messages": [
            history_message("om_synthetic_race", at=now - 30)], "has_more": False}]
        instance = adapter(tmp_path, transport)
        bus = MessageBus(runtime_root=tmp_path)
        published = asyncio.Event()
        bus.subscribe_topic(MessageType.USER_MESSAGE, lambda _: published.set())
        try:
            with caplog.at_level("INFO", logger="looper.agents.channels.lark"):
                await instance.start(bus)
                barrier = transport.before_list if live_first else transport.before_names
                assert barrier is not None
                await asyncio.wait_for(barrier.wait(), 3)
                await transport.incoming.put(_event(
                    chat_id="oc_synthetic_group", chat_type="group", message_id="om_synthetic_race",
                    content="@HelperBot hello", mentions=[BOT_MENTION]))
                if live_first:
                    await asyncio.wait_for(published.wait(), 3)
                    await asyncio.wait_for(transport.live_handled.wait(), 3)
                    assert transport.release_list is not None
                    transport.release_list.set()
                else:
                    await asyncio.wait_for(transport.live_received.wait(), 3)
                    assert transport.release_names is not None
                    transport.release_names.set()
                await catchup_done(instance, transport)
                await asyncio.wait_for(transport.live_handled.wait(), 3)
            (message,) = bus.journal.undelivered()
            assert message.payload["channel_context"]["message_id"] == "om_synthetic_race"
            if live_first:
                assert "duplicates=1" in caplog.text
            assert instance._inbox.processed("om_synthetic_race")
            await asyncio.gather(*instance._reactions._pending)
            assert len([call for call in transport.calls if call[0] == "react"]) == 1
        finally:
            await instance.stop()
            await bus.shutdown()

    asyncio.run(asyncio.wait_for(run(), 5))


def test_gap_group_without_bot_mention_is_not_answered(tmp_path: Path) -> None:
    async def run() -> None:
        now = time.time()
        seed_boundary(tmp_path, now - 60)
        transport = HistoryTransport()
        transport.pages["oc_synthetic_group"] = [{"chat_type": "group", "messages": [
            history_message("om_synthetic_unmentioned", at=now - 30, mentioned=False)], "has_more": False}]
        instance = adapter(tmp_path, transport)
        bus = MessageBus(runtime_root=tmp_path)
        try:
            await instance.start(bus)
            await catchup_done(instance, transport)
            assert bus.journal.undelivered() == []
            assert transport.calls == []
        finally:
            await instance.stop()
            await bus.shutdown()

    asyncio.run(asyncio.wait_for(run(), 5))


def test_history_message_older_than_cap_is_not_answered_and_counted(
    tmp_path: Path, caplog: pytest.LogCaptureFixture,
) -> None:
    from looper.agents.channels.lark import _CATCHUP_MAX_AGE

    async def run() -> None:
        now = time.time()
        seed_boundary(tmp_path, now - 60)
        transport = HistoryTransport()
        transport.pages["oc_synthetic_group"] = [{"chat_type": "group", "messages": [
            history_message("om_synthetic_old", at=now - _CATCHUP_MAX_AGE - 1)], "has_more": False}]
        instance = adapter(tmp_path, transport)
        bus = MessageBus(runtime_root=tmp_path)
        try:
            with caplog.at_level("INFO", logger="looper.agents.channels.lark"):
                await instance.start(bus)
                await catchup_done(instance, transport)
            assert bus.journal.undelivered() == []
            assert transport.calls == []
            assert "messages_found=1" in caplog.text
            assert "too_old=1" in caplog.text
            assert "handled=0" in caplog.text
        finally:
            await instance.stop()
            await bus.shutdown()

    asyncio.run(asyncio.wait_for(run(), 5))


def test_one_chat_list_failure_does_not_block_other_chats(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    from looper.agents.channels.lark_cli import LarkCliError

    async def chats(self: LarkChannelAdapter) -> list[str]:
        return ["oc_synthetic_unavailable", "oc_synthetic_group"]

    monkeypatch.setattr(LarkChannelAdapter, "_catchup_chats", chats)

    async def run() -> None:
        now = time.time()
        seed_boundary(tmp_path, now - 60)
        transport = HistoryTransport()
        transport.pages["oc_synthetic_unavailable"] = [LarkCliError("synthetic permission denied")]
        transport.pages["oc_synthetic_group"] = [{"chat_type": "group", "messages": [
            history_message("om_synthetic_other", at=now - 30)], "has_more": False}]
        instance = adapter(tmp_path, transport)
        bus = MessageBus(runtime_root=tmp_path)
        try:
            with caplog.at_level("INFO", logger="looper.agents.channels.lark"):
                await instance.start(bus)
                await catchup_done(instance, transport)
            (message,) = bus.journal.undelivered()
            assert message.payload["channel_context"]["message_id"] == "om_synthetic_other"
            assert "chats_scanned=2" in caplog.text
            assert "failures=1" in caplog.text
            assert any(record.levelname == "WARNING" for record in caplog.records)
            assert not instance._collector_task.done()
        finally:
            await instance.stop()
            await bus.shutdown()

    asyncio.run(asyncio.wait_for(run(), 5))


def test_boundary_and_live_receipts_survive_a_new_adapter_object(
    tmp_path: Path, caplog: pytest.LogCaptureFixture,
) -> None:
    from looper.agents.channels.lark_inbox import LarkInbox

    async def run() -> None:
        bus = MessageBus(runtime_root=tmp_path)
        first_transport = HistoryTransport()
        first = adapter(tmp_path, first_transport)
        second: LarkChannelAdapter | None = None
        first_stopped = False
        try:
            await first.start(bus)
            await catchup_done(first, first_transport)
            await first._handle_lark_event(_event(
                chat_id="oc_synthetic_group", chat_type="group", message_id="om_synthetic_live",
                content="@HelperBot hello", mentions=[BOT_MENTION]))
            before_stop = time.time()
            await first.stop()
            first_stopped = True
            inbox = LarkInbox(tmp_path / ".local" / "lark" / "channel-lark" / "inbox.sqlite3")
            try:
                stopped_at = inbox.boundary
                assert stopped_at is not None and before_stop <= stopped_at <= time.time()
                assert inbox.processed("om_synthetic_live")
            finally:
                inbox.close()
            second_transport = HistoryTransport()
            second_transport.pages["oc_synthetic_group"] = [{"chat_type": "group", "messages": [
                history_message("om_synthetic_live", at=stopped_at),
                history_message("om_synthetic_restarted", at=time.time())], "has_more": False}]
            second = adapter(tmp_path, second_transport)
            with caplog.at_level("INFO", logger="looper.agents.channels.lark"):
                await second.start(bus)
                await catchup_done(second, second_transport)
            ids = [message.payload["channel_context"]["message_id"] for message in bus.journal.undelivered()]
            assert ids == ["om_synthetic_live", "om_synthetic_restarted"]
            assert second_transport.list_calls[0]["start"] == pytest.approx(stopped_at - 10)
            assert "duplicates=1" in caplog.text
        finally:
            if not first_stopped:
                await first.stop()
            if second is not None:
                await second.stop()
            await bus.shutdown()

    asyncio.run(asyncio.wait_for(run(), 5))


def test_expired_boundary_skips_history_entirely(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    from looper.agents.channels.lark import _CATCHUP_MAX_AGE

    async def run() -> None:
        seed_boundary(tmp_path, time.time() - _CATCHUP_MAX_AGE - 1)
        transport = HistoryTransport()
        instance = adapter(tmp_path, transport)
        bus = MessageBus(runtime_root=tmp_path)
        try:
            with caplog.at_level("INFO", logger="looper.agents.channels.lark"):
                await instance.start(bus)
                await catchup_done(instance, transport)
            assert transport.list_calls == []
            assert bus.journal.undelivered() == []
            assert "boundary_expired=True" in caplog.text
        finally:
            await instance.stop()
            await bus.shutdown()

    asyncio.run(asyncio.wait_for(run(), 5))


def test_catchup_pages_and_chats_are_bounded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:

    async def chats(self: LarkChannelAdapter) -> list[str]:
        return ["oc_synthetic_group", "oc_synthetic_unscanned"]

    monkeypatch.setattr(LarkChannelAdapter, "_catchup_chats", chats)
    monkeypatch.setattr("looper.agents.channels.lark._CATCHUP_MAX_CHATS", 1)
    monkeypatch.setattr("looper.agents.channels.lark._CATCHUP_MAX_PAGES", 2)

    async def run() -> None:
        seed_boundary(tmp_path, time.time() - 60)
        transport = HistoryTransport()
        transport.pages["oc_synthetic_group"] = [
            {"chat_type": "group", "messages": [], "has_more": True, "page_token": "synthetic-next"},
            {"chat_type": "group", "messages": [], "has_more": True, "page_token": "synthetic-last"},
            {"chat_type": "group", "messages": [], "has_more": False},
        ]
        instance = adapter(tmp_path, transport)
        bus = MessageBus(runtime_root=tmp_path)
        try:
            await instance.start(bus)
            await catchup_done(instance, transport)
            assert len(transport.list_calls) == 2
            assert [call["page_token"] for call in transport.list_calls] == ["", "synthetic-next"]
            assert {call["chat_id"] for call in transport.list_calls} == {"oc_synthetic_group"}
            assert len(transport.pages["oc_synthetic_group"]) == 1
        finally:
            await instance.stop()
            await bus.shutdown()

    asyncio.run(asyncio.wait_for(run(), 5))


def test_listable_p2p_chat_is_recovered_without_a_mention(tmp_path: Path) -> None:
    async def run() -> None:
        now = time.time()
        seed_boundary(tmp_path, now - 60)
        transport = HistoryTransport()
        transport.pages["oc_synthetic_group"] = [{"chat_type": "p2p", "messages": [
            history_message("om_synthetic_direct", at=now - 30, mentioned=False)], "has_more": False}]
        instance = adapter(tmp_path, transport)
        bus = MessageBus(runtime_root=tmp_path)
        try:
            await instance.start(bus)
            await catchup_done(instance, transport)
            (message,) = bus.journal.undelivered()
            assert message.payload["channel_context"]["chat_type"] == "p2p"
            assert message.payload["text"] == "hello"
        finally:
            await instance.stop()
            await bus.shutdown()

    asyncio.run(asyncio.wait_for(run(), 5))


def test_durable_message_receipts_are_bounded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from looper.agents.channels.lark_inbox import LarkInbox

    monkeypatch.setattr("looper.agents.channels.lark_inbox.PROCESSED_KEPT", 2)
    path = tmp_path / "inbox.sqlite3"
    inbox = LarkInbox(path)
    try:
        for message_id in ("om_synthetic_first", "om_synthetic_second", "om_synthetic_third"):
            inbox.finish(message_id)
    finally:
        inbox.close()
    reopened = LarkInbox(path)
    try:
        assert not reopened.processed("om_synthetic_first")
        assert reopened.processed("om_synthetic_second")
        assert reopened.processed("om_synthetic_third")
    finally:
        reopened.close()


def test_stopping_a_disconnected_consumer_preserves_the_last_checkpoint(tmp_path: Path) -> None:
    from looper.agents.channels.lark_inbox import LarkInbox

    async def run() -> None:
        now = time.time()
        seed_boundary(tmp_path, now - 60)
        transport = HistoryTransport()
        transport.pages["oc_synthetic_group"] = [{"chat_type": "group", "messages": [], "has_more": False}]
        instance = adapter(tmp_path, transport)
        bus = MessageBus(runtime_root=tmp_path)
        stopped = False
        try:
            await instance.start(bus)
            await catchup_done(instance, transport)
            assert instance._inbox is not None
            last_checkpoint = now - 30
            instance._inbox.checkpoint(last_checkpoint)
            transport.consumer_started.clear()
            await instance.stop()
            stopped = True
            reopened = LarkInbox(tmp_path / ".local" / "lark" / "channel-lark" / "inbox.sqlite3")
            try:
                assert reopened.boundary == last_checkpoint
            finally:
                reopened.close()
        finally:
            if not stopped:
                await instance.stop()
            await bus.shutdown()

    asyncio.run(asyncio.wait_for(run(), 5))


def test_idle_heartbeat_persists_only_after_catchup_finishes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from looper.agents.channels.lark_inbox import LarkInbox

    monkeypatch.setattr("looper.agents.channels.lark._HEARTBEAT_SECONDS", 0.01)

    async def run() -> None:
        boundary = time.time() - 60
        seed_boundary(tmp_path, boundary)
        transport = HistoryTransport()
        transport.before_list, transport.release_list = asyncio.Event(), asyncio.Event()
        transport.pages["oc_synthetic_group"] = [{"chat_type": "group", "messages": [], "has_more": False}]
        instance = adapter(tmp_path, transport)
        bus = MessageBus(runtime_root=tmp_path)
        checkpoint_saved = asyncio.Event()
        try:
            await instance.start(bus)
            assert instance._inbox is not None
            checkpoint = instance._inbox.checkpoint

            def save(at: float) -> None:
                checkpoint(at)
                checkpoint_saved.set()

            monkeypatch.setattr(instance._inbox, "checkpoint", save)
            await asyncio.wait_for(transport.before_list.wait(), 3)
            # Let several heartbeat intervals elapse while history remains blocked.
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(checkpoint_saved.wait(), 0.05)
            assert instance._inbox.boundary == boundary
            transport.release_list.set()
            await catchup_done(instance, transport)
            await asyncio.wait_for(checkpoint_saved.wait(), 3)
            reopened = LarkInbox(tmp_path / ".local" / "lark" / "channel-lark" / "inbox.sqlite3")
            try:
                assert reopened.boundary is not None
                assert boundary < reopened.boundary <= time.time()
            finally:
                reopened.close()
            assert bus.journal.undelivered() == []
            assert transport.calls == []
        finally:
            await instance.stop()
            await bus.shutdown()

    asyncio.run(asyncio.wait_for(run(), 5))
