"""Task reports held by drain retain their original reply destination across restart."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telegram import Bot
from telegram.request import BaseRequest

from looper.agents.bus import MessageBus
from looper.agents.channels.allowlist import SenderAllowlist
from looper.agents.channels.base import DeliveryFailure
from looper.agents.channels.lark import LarkChannelAdapter
from looper.agents.channels.telegram import TelegramChannelAdapter
from looper.agents.drain import DrainScope, DrainState
from looper.agents.message import MANAGER_ADDRESS, TASK_ROUTER_ADDRESS, MessageEnvelope, MessageType
from looper.agents.tiers.manager import ManagerAgent, ManagerConfig
from looper.agents.tiers.task import Task, TaskRouter
from looper.db import tasks as db_tasks
from tests.db_support import until
from tests.test_lark_transport import StubTransport


class _TelegramAPI(BaseRequest):
    """Exercise Bot's real serialization and response handling without network access."""

    def __init__(self):
        self.sent = []

    @property
    def read_timeout(self):
        return 1

    async def initialize(self):
        pass

    async def shutdown(self):
        pass

    async def do_request(self, url, method, request_data=None, **kwargs):
        operation = url.rsplit("/", 1)[-1]
        if operation == "getMe":
            result = {"id": 1, "is_bot": True, "first_name": "Synthetic"}
        else:
            assert operation == "sendMessage", operation
            values = request_data.parameters
            self.sent.append(values)
            result = {"message_id": 42, "date": 0, "chat": {"id": values["chat_id"], "type": "private"},
                      "text": values["text"]}
        return 200, json.dumps({"ok": True, "result": result}).encode()


@pytest.mark.parametrize("channel,chat,channel_id", [
    ("telegram", "99", "channel-telegram"), ("telegram", "-99", "channel-telegram-2"),
    ("lark", "oc_group", "channel-lark"), ("lark", "oc_dm", "channel-lark-2"),
    ("lark_bot", "oc_group", "channel-lark_bot"),
])
@pytest.mark.parametrize("kind", [MessageType.TASK_ACCEPTED, MessageType.GOAL_STATUS])
@pytest.mark.parametrize("restart", [False, True])
def test_task_progress_reaches_original_chat_once(agent_db, tmp_path, monkeypatch, channel, chat, channel_id,
                                                 kind, restart):
    text = "任务已分解为 3 个步骤，开始执行..." if kind is MessageType.TASK_ACCEPTED else "进度更新: 1/3 完成"
    route = {"chat_id": chat, **({"reply_to": "om_question"} if channel != "telegram" else {})}
    api = _TelegramAPI()
    bot = Bot("0:synthetic", request=api, get_updates_request=_TelegramAPI()) if channel == "telegram" else None
    transport = StubTransport()

    def adapter(bus):
        allowlist = SenderAllowlist(channel_id, frozenset({"7"}))
        if channel == "telegram":
            result = TelegramChannelAdapter(allowlist=allowlist, channel_id=channel_id)
            result._app = SimpleNamespace(bot=bot)
        else:
            result = LarkChannelAdapter(allowlist=allowlist, channel_type=channel, channel_id=channel_id,
                                        transport=transport, runtime_root=tmp_path)
        result._bus, result._running = bus, True
        return result

    async def consume(bus, outbound):
        outbound._outbound_task = asyncio.create_task(outbound._outbound_loop())
        try:
            await until(lambda: bool(api.sent or transport.calls), timeout=3)
            await until(lambda: not bus.journal.undelivered(), timeout=3)
            assert bus.pending_count(channel_id) == 0
        finally:
            outbound._outbound_task.cancel()
            await asyncio.gather(outbound._outbound_task, return_exceptions=True)

    async def run():
        bus = MessageBus(runtime_root=tmp_path)
        await bus.start()
        drain = DrainState()
        held = asyncio.Event()
        wait = drain.wait_serving

        async def waiting():
            held.set()
            await wait()

        monkeypatch.setattr(drain, "wait_serving", waiting)
        manager = ManagerAgent(config=ManagerConfig(work_dir=tmp_path), runtime_root=tmp_path, drain=drain)
        outbound = adapter(bus)
        if restart:
            drain.begin(DrainScope.TURNS)
        else:
            # A live correlation wins over durable routing, including the Lark group's existing broadcast.
            outbound._correlation_chat["c1"] = 101 if channel == "telegram" else "oc_live"
            if channel != "telegram":
                outbound._group_message["c1"] = "om_live"
        router = TaskRouter(runtime_root=tmp_path)
        router._bus = bus
        router._tasks["t1"] = Task.from_row("t1", {
            "requirement": "finish", "project_id": "p1", "correlation_id": "c1",
            "origin_channel_id": channel_id, "origin_reply_route": route,
        })
        report = MessageEnvelope.create(
            source=TASK_ROUTER_ADDRESS, target=TASK_ROUTER_ADDRESS, message_type=kind,
            payload={"task_id": "t1", "status": "executing", "step_count": 3, "summary": text},
            correlation_id="c1", metadata={"origin_channel_id": channel_id})
        await manager.start(bus)
        try:
            if kind is MessageType.TASK_ACCEPTED:
                await router._handle_task_accepted(report)
            else:
                await router._handle_planner_status(report)
            if restart:
                await asyncio.wait_for(held.wait(), 2)
                assert not api.sent and not transport.calls
                (pending,) = bus.journal.undelivered()
                assert pending.message_type is kind and pending.target == MANAGER_ADDRESS
            else:
                await consume(bus, outbound)
        finally:
            await manager.stop()
            await bus.shutdown()

        if restart:
            bus = MessageBus(runtime_root=tmp_path)
            await bus.start()
            manager = ManagerAgent(config=ManagerConfig(work_dir=tmp_path), runtime_root=tmp_path)
            outbound = adapter(bus)
            assert not outbound._correlation_chat
            await manager.start(bus)
            try:
                await consume(bus, outbound)
            finally:
                await manager.stop()
                await bus.shutdown()

        if channel == "telegram":
            (sent,) = api.sent
            assert sent["text"] == text
            assert sent["chat_id"] == (int(chat) if restart else 101)
        elif restart:
            assert transport.calls == [("reply", {"message_id": "om_question", "markdown": text})]
        else:
            assert transport.calls == [("send", {"chat_id": "oc_live", "markdown": text})]

        # Another startup replays the same journal; the acknowledged task report cannot send again.
        bus = MessageBus(runtime_root=tmp_path)
        await bus.start()
        try:
            assert not bus.journal.undelivered()
            assert bus.pending_count("manager") == bus.pending_count(channel_id) == 0
        finally:
            await bus.shutdown()

    async def main():
        if bot is not None:
            async with bot:
                await run()
        else:
            await run()

    asyncio.run(asyncio.wait_for(main(), 10))


def test_start_over_keeps_chat_progress_silent_while_serving(agent_db, planner_claude, tmp_path, monkeypatch):
    from tests.test_planner_agent import plan, step

    channel_id = "channel-lark"
    route = {"chat_id": "oc_original", "reply_to": "om_original"}
    agent_db.store.tasks["t1"] = {
        "requirement": "finish", "project_id": "p1", "work_dir": str(tmp_path), "status": "stopped",
        "correlation_id": "c1", "origin_channel_id": channel_id, "origin_reply_route": route,
    }
    monkeypatch.setattr(db_tasks, "reset_task_for_restart", AsyncMock(return_value=True))
    claude = planner_claude(plan(step("s1")))

    async def run():
        bus = MessageBus(runtime_root=tmp_path)
        await bus.start()
        router = TaskRouter(runtime_root=tmp_path, claude_bin=claude.bin)
        manager = ManagerAgent(config=ManagerConfig(work_dir=tmp_path), runtime_root=tmp_path, drain=DrainState())
        manager._bus = bus
        await router.start(bus)
        try:
            await bus.publish(MessageEnvelope.create(
                source=MANAGER_ADDRESS, target=TASK_ROUTER_ADDRESS, message_type=MessageType.TASK_RESTART,
                payload={"task_id": "t1", "mode": "start_over"}))
            queue = bus.get_queue("manager")
            reports = [await asyncio.wait_for(queue.get(), 5) for _ in range(2)]
            assert [report.message_type for report in reports] == [MessageType.TASK_ACCEPTED, MessageType.GOAL_STATUS]
            await manager._handle_task_accepted(reports[0])
            await manager._handle_goal_status(reports[1])
            assert bus.pending_count(channel_id) == 0
            # The persisted route supplements each report without opting it into chat progress.
            assert [report.metadata.get("origin_channel_id") for report in reports] == [None, ""]
            assert all(report.metadata["reply_route"] == route for report in reports)
        finally:
            await router.stop()
            await bus.shutdown()

    asyncio.run(asyncio.wait_for(run(), 15))


@pytest.mark.parametrize("kind,payload,caches", [
    (MessageType.STATUS_UPDATE, {"text": "Task progress"}, False),
    (MessageType.AGENT_REPLY, {"text": "Answer"}, True),
    (MessageType.STATUS_UPDATE, {"phase": "thinking"}, True),
])
@pytest.mark.parametrize("chat", ["99", "invalid"])
def test_telegram_restored_route_only_caches_answers_and_phases(kind, payload, caches, chat):
    api = _TelegramAPI()
    adapter = TelegramChannelAdapter(allowlist=SenderAllowlist("channel-telegram", frozenset({"7"})))

    async def run():
        async with Bot("0:synthetic", request=api, get_updates_request=_TelegramAPI()) as bot:
            adapter._app = SimpleNamespace(bot=bot)
            first = MessageEnvelope.create(
                source=MANAGER_ADDRESS, target=adapter._address, message_type=kind,
                payload=payload, correlation_id="restored", metadata={"reply_route": {"chat_id": chat}})
            receipt = await adapter.deliver(first)
            assert receipt.success is (chat == "99")
            if chat == "invalid":
                assert receipt.failure is DeliveryFailure.NO_CHAT
            followup = MessageEnvelope.create(
                source=MANAGER_ADDRESS, target=adapter._address, message_type=MessageType.STATUS_UPDATE,
                payload={"text": "Next progress"}, correlation_id="restored")
            receipt = await adapter.deliver(followup)
            cached = caches and chat == "99"
            assert receipt.success is cached
            if not cached:
                assert receipt.failure is DeliveryFailure.NO_CHAT
            assert ("restored" in adapter._correlation_chat) is cached
            assert len(api.sent) == (int(chat == "99") + int(cached))

    asyncio.run(asyncio.wait_for(run(), 5))
