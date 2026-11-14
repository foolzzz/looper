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


def raw_message(message: dict[str, Any]) -> dict[str, Any]:
    import json

    return {**message, "body": {"content": json.dumps({"text": message["content"]})},
            "thread_replies": [raw_message(reply) for reply in message.get("thread_replies") or []]}


async def seed_boundary(root: Path, at: float) -> None:
    from looper.agents.channels.lark_inbox import LarkInbox

    inbox = await LarkInbox.open(root / ".local" / "lark" / "channel-lark" / "inbox.sqlite3")
    try:
        await inbox.checkpoint(at)
    finally:
        await inbox.close()


async def catchup_done(instance: LarkChannelAdapter, transport: HistoryTransport, *, timeout: float = 3) -> None:
    await asyncio.wait_for(transport.consumer_started.wait(), 3)
    assert instance._catchup_task is not None
    await asyncio.wait_for(instance._catchup_task, timeout)


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
        await seed_boundary(tmp_path, now - 60)
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


@pytest.mark.parametrize("thread", [False, True])
@pytest.mark.parametrize("boundary_offset", [20, 0])
def test_minute_history_overlaps_restart_window_without_ending_thread_scan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, thread: bool, boundary_offset: int,
) -> None:
    from datetime import datetime

    async def run() -> None:
        now = datetime(2026, 11, 12, 18, 31, 10).timestamp()
        monkeypatch.setattr("looper.agents.channels.lark.time", lambda: now)
        await seed_boundary(tmp_path, now - boundary_offset)  # Starts at 18:30:40 or exactly 18:31:00.
        messages = [history_message(f"om_minute_{minute}", at=now,
                                   create_time=f"2026-11-12 18:{minute}")
                    for minute in ("29", "30", "31", "32")]
        transport = HistoryTransport()
        transport.pages["oc_synthetic_group"] = [{
            "messages": [] if thread else messages, "chat_type": "group", "has_more": False}]
        tokens = []

        async def roots(chat_id, *, page_token=""):
            return {"roots": [{"thread_id": "omt_minute", "create_time": str(int(now * 1000))}], "has_more": False}

        async def replies(thread_id, *, page_token=""):
            tokens.append(page_token)
            # A page in the start minute must not hide another in-window reply.
            return {"messages": [messages[1]] if not page_token else [
                history_message("om_same_minute_next_page", at=now, create_time="2026-11-12 18:30"),
                messages[0]], "chat_type": "group", "has_more": not page_token, "page_token": "next"}

        if thread:
            transport.list_threads, transport.list_thread_messages = roots, replies
        instance = adapter(tmp_path, transport)
        bus = MessageBus(runtime_root=tmp_path)
        try:
            await instance.start(bus)
            await catchup_done(instance, transport)
            ids = {m.payload["channel_context"]["message_id"] for m in bus.journal.undelivered()}
            expected = ({"om_minute_30", "om_same_minute_next_page"} if thread else {
                "om_minute_30", "om_minute_31"}) if boundary_offset else (set() if thread else {"om_minute_31"})
            assert ids == expected
            if thread:
                assert tokens == (["", "next"] if boundary_offset else [""])
        finally:
            await instance.stop()
            await bus.shutdown()

    asyncio.run(asyncio.wait_for(run(), 5))


def test_minute_history_overlaps_age_limit_but_excludes_previous_minute(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from datetime import datetime

    async def run() -> None:
        now = datetime(2026, 11, 12, 18, 31, 10).timestamp()
        monkeypatch.setattr("looper.agents.channels.lark.time", lambda: now)
        await seed_boundary(tmp_path, now - 900)
        transport = HistoryTransport()
        transport.pages["oc_synthetic_group"] = [{"messages": [
            history_message("om_cutoff", at=now, create_time="2026-11-12 18:16"),
            history_message("om_before_cutoff", at=now, create_time="2026-11-12 18:15")],
            "chat_type": "group", "has_more": False}]
        instance = adapter(tmp_path, transport)
        bus = MessageBus(runtime_root=tmp_path)
        try:
            await instance.start(bus)
            await catchup_done(instance, transport)
            assert [m.payload["channel_context"]["message_id"] for m in bus.journal.undelivered()] == ["om_cutoff"]
        finally:
            await instance.stop()
            await bus.shutdown()

    asyncio.run(asyncio.wait_for(run(), 5))


@pytest.mark.parametrize("scope", ["search:message", "im:message.reactions:read"])
def test_thread_catchup_never_requests_search_scopes(tmp_path, monkeypatch, scope):
    import json
    from looper.agents.channels import lark_cli
    from looper.agents.channels.lark_transport import CliTransport

    async def run():
        now = time.time()
        await seed_boundary(tmp_path, now - 60)
        calls = []

        async def run_json(argv):
            calls.append(argv)
            if "+messages-search" in argv:
                raise lark_cli.LarkCliError(f"missing scope: {scope}")
            if "/open-apis/im/v1/chats/oc_synthetic_group" in argv:
                return {"data": {"chat_mode": "group"}}
            params = json.loads(argv[argv.index("--params") + 1])
            if params["container_id_type"] == "thread":
                items = [raw_message(history_message("om_reply", at=now - 5,
                                                     parent_id="om_gate", root_id="om_gate"))]
            else:
                items = [{"thread_id": "omt_old", "create_time": str(int((now - 3600) * 1000))}] if "start_time" not in params else []
            return {"data": {"items": items, "has_more": False}}

        monkeypatch.setattr(lark_cli, "run_json", run_json)
        transport = HistoryTransport()
        cli = CliTransport()
        for name in ("list_messages", "list_threads", "list_thread_messages"):
            setattr(transport, name, getattr(cli, name))
        instance = adapter(tmp_path, transport)
        bus = MessageBus(runtime_root=tmp_path)
        try:
            await instance.start(bus)
            await catchup_done(instance, transport)
            (message,) = bus.journal.undelivered()
            assert message.payload["channel_context"]["message_id"] == "om_reply"
            assert not any("+messages-search" in call for call in calls)
            assert (await instance._inbox.debts()) == {}
        finally:
            await instance.stop()
            await bus.shutdown()

    asyncio.run(asyncio.wait_for(run(), 5))


@pytest.mark.parametrize("source", ["chat", "thread", "live"])
def test_thread_reply_keeps_raw_references_for_gate_routing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, source: str,
) -> None:
    from looper.agents import gate_routing
    from looper.agents.channels import lark_cli
    from looper.agents.channels.lark_transport import CliTransport
    from looper.agents.message import manager_address
    from looper.agents.tiers.manager import ManagerAgent
    from types import SimpleNamespace

    async def run() -> None:
        now = time.time()
        await seed_boundary(tmp_path, now - 60)
        reply = history_message("om_answer", at=now - 5, thread_id="omt_gate")
        raw = {"message_id": "om_answer", "parent_id": "om_gate_question", "root_id": "om_thread_root"}

        async def run_json(argv):
            import json

            assert "+messages-search" not in argv
            if "/open-apis/im/v1/chats/oc_synthetic_group" in argv:
                return {"data": {"chat_mode": "group"}}
            params = json.loads(argv[argv.index("--params") + 1])
            if params["container_id_type"] == "thread":
                items = [raw_message({**reply, **raw})] if source == "thread" else []
            elif "start_time" in params:
                items = [raw_message({**reply, **raw})] if source == "chat" else []
            else:
                items = [{"thread_id": "omt_gate", "create_time": str(int((now - 3600) * 1000))}]
            return {"data": {"items": items, "has_more": False}}

        posted = []

        async def find_reference(channel, chat, refs, **kwargs):
            assert refs == ["om_gate_question"]
            return {"task_id": "task-synthetic", "step_key": "g1", "state": "open"}

        async def post_message(*args, **kwargs):
            posted.append((args, kwargs))
            return {"delivered": "delivered"}

        async def task_name(task_id):
            return "Synthetic task"

        monkeypatch.setattr(lark_cli, "run_json", run_json)
        monkeypatch.setattr(gate_routing.db_user_gates, "find_reference", find_reference)
        monkeypatch.setattr(gate_routing, "post_message", post_message)
        monkeypatch.setattr(gate_routing, "task_name", task_name)
        transport = HistoryTransport()
        cli = CliTransport()
        for name in ("list_messages", "list_threads", "list_thread_messages"):
            setattr(transport, name, getattr(cli, name))
        instance = adapter(tmp_path, transport)
        bus = MessageBus(runtime_root=tmp_path)
        try:
            await instance.start(bus)
            await catchup_done(instance, transport)
            if source == "live":
                await instance._handle_lark_event(_event(
                    chat_id="oc_synthetic_group", chat_type="group", message_id="om_answer",
                    content="@HelperBot hello", mentions=[BOT_MENTION],
                    reply_to=raw["parent_id"], root_id=raw["root_id"]))
            (message,) = bus.journal.undelivered()
            assert message.payload["channel_context"]["reply_to"] == raw["parent_id"]
            assert message.payload["channel_context"]["root_id"] == raw["root_id"]
            # Exercise the Manager's real pre-model branch, exactly as live ingress does.
            manager = SimpleNamespace(
                _bus=bus, ops=SimpleNamespace(platform=SimpleNamespace(
                    task_router=SimpleNamespace(has_planner=lambda _: True))),
                agent_address=manager_address("manager-helper"), _acting=lambda _: None)
            manager._route_to_gate = lambda envelope: ManagerAgent._route_to_gate(manager, envelope)

            async def step(name, operation):
                return await operation

            prepared = await ManagerAgent._prepare_turn(
                manager, SimpleNamespace(check=lambda: None, step=step), message)
            assert prepared.turn is None and prepared.launch is None, "a gate reply must never prepare a Manager model turn"
            assert posted[0][0] == ("task-synthetic", "g1", {"text": "hello"})
            assert posted[0][1]["has_planner"] is True
            assert posted[0][1]["origin"]["external_id"] == "om_answer"
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
        await seed_boundary(tmp_path, now - 60)
        nested = history_message("om_nested", at=now - 40, parent_id="om_recent", root_id="om_recent")
        old_reply = history_message("om_old_reply", at=now - 30, parent_id="om_old", root_id="om_old")
        calls = []

        async def run_json(argv):
            calls.append(argv)
            assert "+messages-search" not in argv
            if "/open-apis/im/v1/chats/oc_synthetic_group" in argv:
                return {"data": {"chat_mode": mode}}
            params = json.loads(argv[argv.index("--params") + 1])
            assert params["page_size"] == 50
            token = params.get("page_token")
            if "start_time" in params:
                items = [raw_message(history_message("om_recent", at=now - 50, mentioned=False,
                                                     thread_replies=[nested]))]
                return {"data": {"items": items, "has_more": False}}
            if params["container_id_type"] == "chat":
                assert params["sort_type"] == "ByCreateTimeDesc"
                return {"data": {"items": [{"thread_id": "omt_old" if token else "omt_recent",
                                             "create_time": str(int((now - (3600 if token else 50)) * 1000))}],
                                 "has_more": not token, "page_token": "roots-next"}}
            assert params["container_id_type"] == "thread"
            assert params["sort_type"] == "ByCreateTimeDesc"
            assert "start_time" not in params and "end_time" not in params
            if params["container_id"] == "omt_recent":
                messages = [nested]
                more = False
            elif not token:
                messages = [history_message("om_future", at=now + 60),
                            history_message("om_latest_reply", at=now - 5, parent_id="om_old", root_id="om_old"),
                            history_message("om_unmentioned", at=now - 10, mentioned=False),
                            history_message("om_denied", at=now - 20, sender={"id": "ou_denied", "sender_type": "user"})]
                more = True
            else:
                assert token == "replies-next"
                messages = [old_reply, history_message("om_expired", at=now - 86400)]
                more = True
            return {"data": {"items": [raw_message(m) for m in messages], "has_more": more,
                             "page_token": "replies-next" if not token else "older-than-window"}}

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
            assert (await instance._inbox.processed("om_old_reply"))
            assert not (await instance._inbox.processed("om_future"))
            assert len([c for c in calls if "--params" in c and json.loads(c[c.index("--params") + 1])["container_id_type"] == "thread"]) == 3
            assert all(c[:3] == ["lark-cli", "--profile", "synthetic-profile"] and "bot" in c for c in calls)
        finally:
            await instance.stop()
            await bus.shutdown()

    asyncio.run(asyncio.wait_for(run(), 5))


@pytest.mark.parametrize("mode", ["group", "topic"])
@pytest.mark.parametrize("old_thread_reply", [False, True])
def test_busy_roots_do_not_hide_recent_replies_or_stall_next_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str, old_thread_reply: bool,
) -> None:
    import json
    from looper.agents.channels import lark_cli
    from looper.agents.channels.lark_inbox import LarkInbox
    from looper.agents.channels.lark_transport import CliTransport

    async def run() -> None:
        clock = [time.time()]
        monkeypatch.setattr("looper.agents.channels.lark.time", lambda: clock[0])
        boundary = clock[0] - 60
        await seed_boundary(tmp_path, boundary)
        bus = MessageBus(runtime_root=tmp_path)
        for restart in range(2):
            message_id = f"om_main_{restart}"
            main = history_message(message_id, at=clock[0] - 5)
            reply = history_message("om_buried_reply", at=clock[0] - 10,
                                    parent_id="om_buried_root", root_id="om_buried_root", thread_id="omt_buried")
            root_pages = []

            async def run_json(argv):
                assert "+messages-search" not in argv
                if "/open-apis/im/v1/chats/oc_synthetic_group" in argv:
                    return {"data": {"chat_mode": mode}}
                params = json.loads(argv[argv.index("--params") + 1])
                if "start_time" in params:
                    messages = [main] + ([reply] if old_thread_reply else [])
                    return {"data": {"items": [raw_message(m) for m in messages], "has_more": False}}
                if params["container_id_type"] == "chat":
                    page = int(params.get("page_token", "0"))
                    root_pages.append(page)
                    return {"data": {"items": [{"thread_id": f"omt_new_{page}_{i}",
                                                "create_time": str(int((clock[0] - 3600) * 1000))} for i in range(50)],
                                     "has_more": True, "page_token": str(page + 1)}}
                return {"data": {"items": [raw_message(reply)] if params["container_id"] == "omt_buried" else [],
                                 "has_more": False}}

            monkeypatch.setattr(lark_cli, "run_json", run_json)
            transport = HistoryTransport()
            cli = CliTransport("synthetic-profile")
            transport.list_messages = cli.list_messages
            transport.list_threads = cli.list_threads
            transport.list_thread_messages = cli.list_thread_messages
            instance = adapter(tmp_path, transport)
            try:
                await instance.start(bus)
                await catchup_done(instance, transport)
                ids = [m.payload["channel_context"]["message_id"] for m in bus.journal.undelivered()]
                expected = ["om_main_0"] + (["om_buried_reply"] if old_thread_reply else [])
                if restart:
                    expected.append("om_main_1")
                assert ids == expected
                assert (await instance._inbox.debts()) == {}
                assert root_pages == list(range(5))
                # Keep the consumer up beyond the original boundary's expiry, then stop cleanly.
                clock[0] += 16 * 60
            finally:
                await instance.stop()
            inbox = await LarkInbox.open(tmp_path / ".local" / "lark" / "channel-lark" / "inbox.sqlite3")
            try:
                boundary = inbox.boundary
                assert boundary == clock[0]
            finally:
                await inbox.close()
            clock[0] += 30
        await bus.shutdown()

    asyncio.run(asyncio.wait_for(run(), 8))


@pytest.mark.parametrize("failure", ["root_error", "root_limit"])
def test_only_root_discovery_errors_preserve_debt_while_live_boundary_advances(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str,
) -> None:
    from looper.agents.channels.lark_cli import LarkCliError
    from looper.agents.channels.lark_inbox import LarkInbox

    monkeypatch.setattr("looper.agents.channels.lark._CATCHUP_MAX_PAGES", 1)
    monkeypatch.setattr("looper.agents.channels.lark._HEARTBEAT_SECONDS", 0.01)

    async def run() -> None:
        boundary = time.time() - 60
        await seed_boundary(tmp_path, boundary)
        entered, release, saved = asyncio.Event(), asyncio.Event(), asyncio.Event()
        transport = HistoryTransport()
        transport.pages["oc_synthetic_group"] = [{"messages": [], "chat_type": "group", "has_more": False}]

        async def list_threads(chat_id, *, page_token=""):
            entered.set()
            await release.wait()
            if failure == "root_error":
                raise LarkCliError("synthetic root read failure")
            return {"roots": [], "has_more": True, "page_token": "next"}

        async def list_thread_messages(thread_id, *, page_token=""):
            raise AssertionError("there are no candidate threads")

        transport.list_threads = list_threads
        transport.list_thread_messages = list_thread_messages
        instance = adapter(tmp_path, transport)
        bus = MessageBus(runtime_root=tmp_path)
        try:
            await instance.start(bus)
            checkpoint = instance._inbox.checkpoint

            async def save(at):
                await checkpoint(at)
                saved.set()

            monkeypatch.setattr(instance._inbox, "checkpoint", save)
            await asyncio.wait_for(entered.wait(), 3)
            await asyncio.wait_for(saved.wait(), 1)
            release.set()
            await catchup_done(instance, transport)
            await asyncio.wait_for(saved.wait(), 1)
            assert instance._inbox.boundary > boundary
            assert bool((await instance._inbox.debts())) == (failure == "root_error")
            if failure == "root_error":
                assert (await instance._inbox.debts())["oc_synthetic_group"][0] == boundary - 10
        finally:
            await instance.stop()
            await bus.shutdown()
        inbox = await LarkInbox.open(tmp_path / ".local" / "lark" / "channel-lark" / "inbox.sqlite3")
        try:
            assert inbox.boundary > boundary
            assert ("oc_synthetic_group" in (await inbox.debts())) == (failure == "root_error")
        finally:
            await inbox.close()

    asyncio.run(asyncio.wait_for(run(), 5))


@pytest.mark.parametrize("live_first", [True, False])
def test_live_event_racing_history_is_published_once(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, live_first: bool,
) -> None:
    async def run() -> None:
        now = time.time()
        await seed_boundary(tmp_path, now - 60)
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
            assert (await instance._inbox.processed("om_synthetic_race"))
            await asyncio.gather(*instance._reactions._pending)
            assert len([call for call in transport.calls if call[0] == "react"]) == 1
        finally:
            await instance.stop()
            await bus.shutdown()

    asyncio.run(asyncio.wait_for(run(), 5))


def test_gap_group_without_bot_mention_is_not_answered(tmp_path: Path) -> None:
    async def run() -> None:
        now = time.time()
        await seed_boundary(tmp_path, now - 60)
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
        await seed_boundary(tmp_path, now - 60)
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
        await seed_boundary(tmp_path, now - 60)
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
            inbox = await LarkInbox.open(tmp_path / ".local" / "lark" / "channel-lark" / "inbox.sqlite3")
            try:
                stopped_at = inbox.boundary
                assert stopped_at is not None and before_stop <= stopped_at <= time.time()
                assert (await inbox.processed("om_synthetic_live"))
            finally:
                await inbox.close()
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
        await seed_boundary(tmp_path, time.time() - _CATCHUP_MAX_AGE - 1)
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
        await seed_boundary(tmp_path, time.time() - 60)
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
        await seed_boundary(tmp_path, now - 60)
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
    async def run():
        from looper.agents.channels.lark_inbox import LarkInbox

        monkeypatch.setattr("looper.agents.channels.lark_inbox.PROCESSED_KEPT", 2)
        path = tmp_path / "inbox.sqlite3"
        inbox = await LarkInbox.open(path)
        try:
            for message_id in ("om_synthetic_first", "om_synthetic_second", "om_synthetic_third"):
                await inbox.finish(message_id)
        finally:
            await inbox.close()
        reopened = await LarkInbox.open(path)
        try:
            assert not (await reopened.processed("om_synthetic_first"))
            assert (await reopened.processed("om_synthetic_second"))
            assert (await reopened.processed("om_synthetic_third"))
        finally:
            await reopened.close()

    asyncio.run(run())


def test_stopping_a_disconnected_consumer_preserves_the_last_checkpoint(tmp_path: Path) -> None:
    from looper.agents.channels.lark_inbox import LarkInbox

    async def run() -> None:
        now = time.time()
        await seed_boundary(tmp_path, now - 60)
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
            await instance._inbox.checkpoint(last_checkpoint)
            transport.consumer_started.clear()
            await instance.stop()
            stopped = True
            reopened = await LarkInbox.open(tmp_path / ".local" / "lark" / "channel-lark" / "inbox.sqlite3")
            try:
                assert reopened.boundary == last_checkpoint
            finally:
                await reopened.close()
        finally:
            if not stopped:
                await instance.stop()
            await bus.shutdown()

    asyncio.run(asyncio.wait_for(run(), 5))


def test_idle_heartbeat_persists_while_catchup_is_pending(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from looper.agents.channels.lark_inbox import LarkInbox

    monkeypatch.setattr("looper.agents.channels.lark._HEARTBEAT_SECONDS", 0.01)

    async def run() -> None:
        boundary = time.time() - 60
        await seed_boundary(tmp_path, boundary)
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

            async def save(at: float) -> None:
                await checkpoint(at)
                checkpoint_saved.set()

            monkeypatch.setattr(instance._inbox, "checkpoint", save)
            await asyncio.wait_for(transport.before_list.wait(), 3)
            await asyncio.wait_for(checkpoint_saved.wait(), 1)
            assert instance._inbox.boundary > boundary
            assert "oc_synthetic_group" in (await instance._inbox.debts())
            transport.release_list.set()
            await catchup_done(instance, transport)
            await asyncio.wait_for(checkpoint_saved.wait(), 3)
            reopened = await LarkInbox.open(tmp_path / ".local" / "lark" / "channel-lark" / "inbox.sqlite3")
            try:
                assert reopened.boundary is not None
                assert boundary < reopened.boundary <= time.time()
            finally:
                await reopened.close()
            assert bus.journal.undelivered() == []
            assert transport.calls == []
        finally:
            await instance.stop()
            await bus.shutdown()

    asyncio.run(asyncio.wait_for(run(), 5))


@pytest.mark.parametrize("elapsed", [120, 16 * 60])
def test_failed_chat_debt_survives_live_progress_and_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, elapsed: int,
) -> None:
    from looper.agents.channels.lark_inbox import LarkInbox
    from looper.agents.channels.lark_cli import LarkCliError

    async def chats(self):
        return ["oc_failed", "oc_synthetic_group"]

    monkeypatch.setattr(LarkChannelAdapter, "_catchup_chats", chats)
    monkeypatch.setattr("looper.agents.channels.lark._HEARTBEAT_SECONDS", 0.01)

    async def run():
        clock = [time.time()]
        monkeypatch.setattr("looper.agents.channels.lark.time", lambda: clock[0])
        original = clock[0] - 60
        await seed_boundary(tmp_path, original)
        bus = MessageBus(runtime_root=tmp_path)
        first_transport = HistoryTransport()
        first_transport.pages = {
            "oc_failed": [LarkCliError("missing scope: im:message:readonly")],
            "oc_synthetic_group": [{"messages": [], "chat_type": "group", "has_more": False}],
        }
        first = adapter(tmp_path, first_transport)
        try:
            await first.start(bus)
            await catchup_done(first, first_transport)
            saved = asyncio.Event()
            checkpoint = first._inbox.checkpoint

            async def save(at):
                await checkpoint(at)
                saved.set()

            monkeypatch.setattr(first._inbox, "checkpoint", save)
            clock[0] += elapsed
            await asyncio.wait_for(saved.wait(), 1)
            assert first._inbox.boundary == clock[0]
        finally:
            await first.stop()
        inbox = await LarkInbox.open(tmp_path / ".local/lark/channel-lark/inbox.sqlite3")
        try:
            assert inbox.boundary == clock[0]
            assert (await inbox.debts()) == {"oc_failed": (original - 10, clock[0] - elapsed)}
        finally:
            await inbox.close()
        clock[0] += 30
        transport = HistoryTransport()
        transport.pages = {
            "oc_failed": [{"messages": [history_message("om_retry", at=original + 30)],
                           "chat_type": "group", "has_more": False}],
            "oc_synthetic_group": [{"messages": [history_message("om_new_gap", at=clock[0] - 5)],
                                    "chat_type": "group", "has_more": False}],
        }
        second = adapter(tmp_path, transport)
        try:
            with caplog.at_level("INFO", logger="looper.agents.channels.lark"):
                await second.start(bus)
                await catchup_done(second, transport)
            ids = {m.payload["channel_context"]["message_id"] for m in bus.journal.undelivered()}
            assert "om_new_gap" in ids
            assert ("om_retry" in ids) == (elapsed == 120)
            failed_call, good_call = transport.list_calls
            assert good_call["start"] == clock[0] - 40
            assert failed_call["start"] == (original - 10 if elapsed == 120 else good_call["start"])
            assert (await second._inbox.debts()) == {}
            if elapsed > 900:
                assert "debt_windows_dropped=1" in caplog.text
                assert "oc_failed" in caplog.text
        finally:
            await second.stop()
            await bus.shutdown()

    asyncio.run(asyncio.wait_for(run(), 5))


@pytest.mark.parametrize("failure", ["error", "cancel"])
def test_discovery_window_survives_live_checkpoint(tmp_path, monkeypatch, failure):
    async def run():
        now = time.time()
        await seed_boundary(tmp_path, now - 60)
        transport = HistoryTransport()
        entered = asyncio.Event()

        async def discover():
            entered.set()
            if failure == "error":
                raise RuntimeError("synthetic discovery unavailable")
            await asyncio.Event().wait()

        instance = adapter(tmp_path, transport)
        monkeypatch.setattr(instance, "_catchup_chats", discover)
        bus = MessageBus(runtime_root=tmp_path)
        try:
            await instance.start(bus)
            await asyncio.wait_for(entered.wait(), 1)
            if failure == "error":
                await catchup_done(instance, transport)
            assert (await instance._inbox.discovery())[0] == now - 70
            await instance._inbox.checkpoint(now)
        finally:
            await instance.stop()
        resumed_transport = HistoryTransport()
        resumed_transport.pages["oc_synthetic_group"] = [{
            "chat_type": "group", "messages": [history_message("om_discovered", at=now - 30)], "has_more": False}]
        resumed = adapter(tmp_path, resumed_transport)
        try:
            await resumed.start(bus)
            await catchup_done(resumed, resumed_transport)
            assert resumed_transport.list_calls[0]["start"] == now - 70
            (message,) = bus.journal.undelivered()
            assert message.payload["channel_context"]["message_id"] == "om_discovered"
            assert (await resumed._inbox.discovery()) is None
            assert (await resumed._inbox.debts()) == {}
        finally:
            await resumed.stop()
            await bus.shutdown()

    asyncio.run(asyncio.wait_for(run(), 5))


def test_partly_expired_chat_debt_keeps_recent_portion(tmp_path, monkeypatch, caplog):
    from looper.agents.channels.lark_inbox import LarkInbox

    async def run():
        now = time.time()
        monkeypatch.setattr("looper.agents.channels.lark.time", lambda: now)
        await seed_boundary(tmp_path, now - 10)
        inbox = await LarkInbox.open(tmp_path / ".local/lark/channel-lark/inbox.sqlite3")
        await inbox.defer("oc_synthetic_group", now - 1000, now - 60)
        await inbox.close()
        transport = HistoryTransport()
        transport.pages["oc_synthetic_group"] = [{
            "chat_type": "group", "messages": [history_message("om_debt", at=now - 800)], "has_more": False}]
        instance = adapter(tmp_path, transport)
        bus = MessageBus(runtime_root=tmp_path)
        try:
            with caplog.at_level("INFO", logger="looper.agents.channels.lark"):
                await instance.start(bus)
                await catchup_done(instance, transport)
            assert transport.list_calls[0]["start"] == now - 900
            (message,) = bus.journal.undelivered()
            assert message.payload["channel_context"]["message_id"] == "om_debt"
            assert "debt_windows_dropped=1" in caplog.text
            assert "skipped_seconds=100" in caplog.text
            assert (await instance._inbox.debts()) == {}
        finally:
            await instance.stop()
            await bus.shutdown()

    asyncio.run(asyncio.wait_for(run(), 5))


def test_owed_chat_is_retried_before_new_discovery_at_chat_limit(tmp_path, monkeypatch):
    from looper.agents.channels.lark_inbox import LarkInbox

    monkeypatch.setattr("looper.agents.channels.lark._CATCHUP_MAX_CHATS", 1)

    async def run():
        now = time.time()
        await seed_boundary(tmp_path, now - 10)
        inbox = await LarkInbox.open(tmp_path / ".local/lark/channel-lark/inbox.sqlite3")
        await inbox.defer("oc_owed", now - 100, now - 60)
        await inbox.close()
        transport = HistoryTransport()
        transport.pages["oc_owed"] = [{"chat_type": "group", "messages": [
            history_message("om_owed", at=now - 80)], "has_more": False}]
        instance = adapter(tmp_path, transport)
        bus = MessageBus(runtime_root=tmp_path)
        try:
            await instance.start(bus)
            await catchup_done(instance, transport)
            assert [call["chat_id"] for call in transport.list_calls] == ["oc_owed"]
            (message,) = bus.journal.undelivered()
            assert message.payload["channel_context"]["message_id"] == "om_owed"
            assert set((await instance._inbox.debts())) == {"oc_synthetic_group"}
        finally:
            await instance.stop()
            await bus.shutdown()

    asyncio.run(asyncio.wait_for(run(), 5))


@pytest.mark.parametrize("recent_roots", [30, 600])
def test_thread_discovery_bounds_busy_chats_by_age_and_listing_budget(
    tmp_path, caplog, recent_roots,
):
    async def run():
        now = time.time()
        await seed_boundary(tmp_path, now - 60)
        transport = HistoryTransport()
        transport.pages["oc_synthetic_group"] = [{"messages": [], "chat_type": "group", "has_more": False}]
        root_calls, thread_calls = [], []
        active, peak = 0, 0
        four_started, release = asyncio.Event(), asyncio.Event()

        async def roots(chat_id, *, page_token=""):
            offset = int(page_token or "0")
            root_calls.append(offset)
            items = [{"thread_id": f"omt_synthetic_{i}", "create_time": str(int(
                (now - 3600 if i < recent_roots else now - 2 * 86400) * 1000))}
                     for i in range(offset, min(offset + 50, 600))]
            return {"roots": items,
                    "has_more": offset + 50 < 600, "page_token": str(offset + 50)}

        async def replies(thread_id, *, page_token=""):
            nonlocal active, peak
            thread_calls.append(thread_id)
            active += 1
            peak = max(peak, active)
            if active == 4:
                four_started.set()
            try:
                await release.wait()
                return {"messages": [history_message(f"om_reply_{thread_id}", at=now - 5),
                                     history_message(f"om_old_{thread_id}", at=now - 120)],
                        "chat_type": "group", "has_more": True, "page_token": "older"}
            finally:
                active -= 1

        transport.list_threads, transport.list_thread_messages = roots, replies
        instance = adapter(tmp_path, transport)
        bus = MessageBus(runtime_root=tmp_path)
        try:
            with caplog.at_level("INFO", logger="looper.agents.channels.lark"):
                await instance.start(bus)
                await asyncio.wait_for(four_started.wait(), 3)
                await asyncio.sleep(0)  # let every scheduled listing reach the transport barrier
                assert active == 4
                release.set()
                await catchup_done(instance, transport, timeout=20)
            count = min(recent_roots, 50)
            assert len(thread_calls) == count and len(set(thread_calls)) == count
            assert peak == 4
            assert len(bus.journal.undelivered()) == count
            assert (await instance._inbox.debts()) == {}
            assert root_calls == ([0] if recent_roots == 30 else [0, 50, 100, 150, 200])
            summary = next(r.message for r in caplog.records if r.message.startswith("lark_catchup "))
            assert f"threads_scanned={count}" in summary
            assert f"thread_roots_capped={int(recent_roots == 600)}" in summary
            assert "thread_failures=0" in summary and "failures=0" in summary
            assert f"too_old={count}" in summary
        finally:
            await instance.stop()
            await bus.shutdown()

    asyncio.run(asyncio.wait_for(run(), 30))


@pytest.mark.parametrize("failure", ["page_error", "page_limit", "cursor"])
def test_thread_listing_failure_keeps_read_replies_and_completes_chat(
    tmp_path, caplog, failure,
):
    async def run():
        now = time.time()
        await seed_boundary(tmp_path, now - 60)
        transport = HistoryTransport()
        transport.pages["oc_synthetic_group"] = [{"messages": [], "chat_type": "group", "has_more": False}]

        async def roots(chat_id, *, page_token=""):
            root = {"thread_id": "omt_synthetic_partial", "create_time": str(int((now - 3600) * 1000))}
            return {"roots": [root], "has_more": False}

        async def replies(thread_id, *, page_token=""):
            if page_token and failure == "page_error":
                raise RuntimeError("synthetic next-page failure")
            return {"messages": [history_message(f"om_synthetic_{page_token or 'first'}", at=now - 5)],
                    "chat_type": "group", "has_more": True,
                    "page_token": "" if failure == "cursor" else page_token + "next"}

        transport.list_threads, transport.list_thread_messages = roots, replies
        instance = adapter(tmp_path, transport)
        bus = MessageBus(runtime_root=tmp_path)
        try:
            with caplog.at_level("INFO", logger="looper.agents.channels.lark"):
                await instance.start(bus)
                await catchup_done(instance, transport)
            ids = [m.payload["channel_context"]["message_id"] for m in bus.journal.undelivered()]
            assert "om_synthetic_first" in ids
            assert len(ids) == (5 if failure == "page_limit" else 1)
            assert (await instance._inbox.debts()) == {}
            summary = next(r.message for r in caplog.records if r.message.startswith("lark_catchup "))
            assert "thread_failures=1" in summary and " failures=0" in summary
            assert any("thread=omt_synthetic_partial" in r.message for r in caplog.records if r.levelname == "WARNING")
        finally:
            await instance.stop()
            await bus.shutdown()

    asyncio.run(asyncio.wait_for(run(), 5))


@pytest.mark.parametrize("activity", ["handled", "sent", "sent_readback", "sent_chat", "sent_chat_readback"])
def test_recent_thread_activity_survives_restart_and_recovers_old_thread_once(
    tmp_path, monkeypatch, activity,
):
    import json
    from looper.agents.channels import lark_cli
    from looper.agents.channels.lark_transport import CliTransport
    from looper.agents.task_notice import NotifyTarget, TargetKind

    async def run():
        clock = [time.time()]
        monkeypatch.setattr("looper.agents.channels.lark.time", lambda: clock[0])
        listed = []

        async def run_json(argv):
            assert "--as" in argv and argv[argv.index("--as") + 1] == "bot"
            metadata = {"chat_id": "oc_synthetic_group", "thread_id": "omt_synthetic_active"}
            if "+messages-reply" in argv or "+messages-send" in argv:
                return {"data": {"message_id": "om_synthetic_sent", **(
                    metadata if activity in {"sent", "sent_chat"} else {})}}
            if "/open-apis/im/v1/messages/om_synthetic_sent" in argv:
                return {"data": {"items": [{"message_id": "om_synthetic_sent", **metadata}]}}
            if "/open-apis/im/v1/chats/oc_synthetic_group" in argv:
                return {"data": {"chat_mode": "group"}}
            params = json.loads(argv[argv.index("--params") + 1])
            if "start_time" in params:
                messages = []
            elif params["container_id_type"] == "chat":
                # An old root without our activity must not be a candidate, even on a later page.
                return {"data": {"items": [{"thread_id": "omt_synthetic_unrelated",
                    "create_time": str(int((clock[0] - 8 * 86400) * 1000))}],
                    "has_more": True, "page_token": "older"}}
            else:
                listed.append(params["container_id"])
                messages = [history_message("om_synthetic_new_reply", at=clock[0] - 5,
                                            thread_id="omt_synthetic_active")]
            return {"data": {"items": [raw_message(message) for message in messages], "has_more": False}}

        monkeypatch.setattr(lark_cli, "run_json", run_json)

        def transport():
            cli, stand_in = CliTransport("synthetic-profile"), HistoryTransport()
            cli.available = lambda: True
            cli.consumer_started = stand_in.consumer_started
            cli.events, cli.names, cli.react = stand_in.events, stand_in.names, stand_in.react
            return cli

        bus = MessageBus(runtime_root=tmp_path)
        first_transport = transport()
        first = adapter(tmp_path, first_transport)
        try:
            await first.start(bus)
            await catchup_done(first, first_transport)
            if activity == "handled":
                assert await first._handle_lark_event(_event(
                    chat_id="oc_synthetic_group", chat_type="group", message_id="om_synthetic_handled",
                    content="@HelperBot hello", mentions=[BOT_MENTION], thread_id="omt_synthetic_active")) == "handled"
            else:
                receipt = await first.post(NotifyTarget(TargetKind.CHAT, "oc_synthetic_group"),
                                           "Synthetic reply", reply_to=(
                                               None if activity.startswith("sent_chat") else "om_synthetic_old_root"))
                assert receipt.success
            assert (await first._inbox.recent_threads("oc_synthetic_group", clock[0])) == ["omt_synthetic_active"]
        finally:
            await first.stop()
        clock[0] += 3 * 86400
        await seed_boundary(tmp_path, clock[0] - 60)
        for _ in range(2):
            current_transport = transport()
            current = adapter(tmp_path, current_transport)
            try:
                await current.start(bus)
                await catchup_done(current, current_transport)
                assert (await current._inbox.debts()) == {}
            finally:
                await current.stop()
        try:
            ids = [m.payload["channel_context"]["message_id"] for m in bus.journal.undelivered()]
            assert ids.count("om_synthetic_new_reply") == 1
            assert listed == ["omt_synthetic_active", "omt_synthetic_active"]
        finally:
            await bus.shutdown()

    asyncio.run(asyncio.wait_for(run(), 5))


def test_thread_activity_is_durable_scoped_recent_and_bounded(tmp_path, monkeypatch):
    async def run():
        from looper.agents.channels.lark_inbox import LarkInbox

        monkeypatch.setattr("looper.agents.channels.lark_inbox.THREADS_KEPT", 2)
        path, now = tmp_path / "inbox.sqlite3", time.time()
        inbox = await LarkInbox.open(path)
        try:
            await inbox.remember_thread("chat-a", "expired", now - 8 * 86400)
            assert (await inbox.recent_threads("chat-a", now)) == []
            await inbox.remember_thread("chat-a", "active", now - 3 * 86400)
            await inbox.remember_thread("chat-b", "other", now - 2 * 86400)
            await inbox.remember_thread("chat-a", "active", now)  # refreshing does not consume another slot
            await inbox.remember_thread("chat-a", "new", now + 1)
        finally:
            await inbox.close()
        reopened = await LarkInbox.open(path)
        try:
            assert (await reopened.recent_threads("chat-a", now + 1)) == ["new", "active"]
            assert (await reopened.recent_threads("chat-b", now + 1)) == []
            assert (await reopened.recent_threads("chat-a", now + 8 * 86400)) == []
        finally:
            await reopened.close()

    asyncio.run(run())


@pytest.mark.parametrize("failure", ["metadata_read", "store"])
def test_sent_thread_bookkeeping_failure_does_not_retry_successful_send(monkeypatch, failure):
    from looper.agents.channels import lark_cli
    from looper.agents.channels.lark_transport import CliTransport

    async def run():
        sends = []

        async def run_json(argv):
            if "+messages-reply" in argv:
                sends.append(argv)
                metadata = {"chat_id": "chat-synthetic", "thread_id": "thread-synthetic"} if failure == "store" else {}
                return {"data": {"message_id": "message-synthetic", **metadata}}
            raise lark_cli.LarkCliError("synthetic metadata read failure")

        async def remember(*args):
            raise RuntimeError("synthetic store failure")

        monkeypatch.setattr(lark_cli, "run_json", run_json)
        cli = CliTransport()
        cli.on_thread_activity = remember
        assert await cli.reply("message-parent", text="Synthetic reply", return_id=True) == "message-synthetic"
        assert len(sends) == 1

    asyncio.run(asyncio.wait_for(run(), 5))
