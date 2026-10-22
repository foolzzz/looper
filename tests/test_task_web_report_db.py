"""A web task's reports land in the chat thread it was asked in, after its answer stream has ended. Runs only
when LOOPER_TEST_DATABASE_URL points at a disposable database."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from looper.agents.channels.web import WebChannelAdapter
from looper.agents.message import MessageEnvelope, MessageType, TASK_ROUTER_ADDRESS, channel_address
from looper.agents.ops import Turn
from looper.agents.tiers.manager import ManagerAgent, ManagerConfig
from tests.test_ops_tasks_db import LARK, _Chat, _project
from tests.test_usage_api_db import DSN, _run

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")

WEB = channel_address("web", "channel-web")


async def _create(chat: _Chat, *, web: bool) -> dict[str, Any]:
    if web:
        envelope = MessageEnvelope.create(
            source=WEB, target=WEB, message_type=MessageType.USER_MESSAGE,
            payload={"text": "确认", "channel_context": {"session_id": "s-web"}},
            correlation_id="c-web", metadata={"origin_channel_id": "channel-web"})
        turn = Turn(channel="web", chat="s-web", message_id=envelope.message_id, envelope=envelope)
        chat.notes.append(chat.ops.begin_turn(turn))
        try:
            return await chat.ops.call("looper_task_create", {"requirement": "build", "title": "计算器", "project": "calc"})
        finally:
            chat.ops.end_turn(turn)
    (answer,) = await chat.say("确认", ("looper_task_create", {"requirement": "build", "title": "计算器", "project": "calc"}))
    return answer


def test_create_records_the_web_thread_only_for_web(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from looper.db import tasks

    async def test(pool: Any) -> None:
        await _project(pool)
        chat = _Chat(tmp_path)
        web = await _create(chat, web=True)
        lark = await _create(chat, web=False)
        assert (await tasks.get_task(web["result"]["task_id"], pool=pool))["origin_thread"] == "s-web"
        assert (await tasks.get_task(lark["result"]["task_id"], pool=pool))["origin_thread"] is None

    _run(test, monkeypatch)


def _report(kind: MessageType, payload: dict[str, Any], **meta: Any) -> MessageEnvelope:
    return MessageEnvelope.create(
        source=TASK_ROUTER_ADDRESS, target=channel_address("web"), message_type=kind, payload=payload,
        correlation_id="c1", metadata={"origin_channel_id": "channel-web", "origin_thread": "s-web",
                                       "task_title": "计算器", **meta})


async def _relay(manager: ManagerAgent, adapter: WebChannelAdapter, envelope: MessageEnvelope) -> list[Any]:
    """Hand the report to the Manager and deliver what it publishes to the web channel."""
    bus = manager._bus
    queue = bus.get_queue("channel-web")
    before = queue.qsize()
    if envelope.message_type == MessageType.TASK_ACCEPTED:
        await manager._handle_task_accepted(envelope)
    else:
        await manager._handle_goal_status(envelope)
    receipts = []
    for _ in range(queue.qsize() - before):
        receipts.append(await adapter.deliver(queue.get_nowait()))
    return receipts


def test_reports_after_the_answer_ended_reach_the_thread(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from looper.agents.bus import MessageBus
    from looper.db import chat as db_chat

    async def test(pool: Any) -> None:
        bus = MessageBus(runtime_root=tmp_path)
        adapter = WebChannelAdapter()
        adapter._bus = bus
        manager = ManagerAgent(config=ManagerConfig(work_dir=tmp_path), runtime_root=tmp_path)
        manager._bus = bus
        await db_chat.save_message(session_id="s-web", message_id="m0", role="user", content="go", pool=pool)

        # The asking message's stream delivered its answer and was dropped.
        adapter._sse_queues["c1"] = asyncio.Queue()
        adapter._sse_queues["c1"].put_nowait({"kind": "answer.final", "payload": {}})
        async for _ in adapter.iter_sse_events("c1"):
            pass
        assert "c1" not in adapter._sse_queues

        live = adapter.iter_chat_events("s-web")  # a page open before the end
        receipts = [
            *await _relay(manager, adapter, _report(MessageType.TASK_ACCEPTED, {"task_id": "t", "step_count": 3})),
            *await _relay(manager, adapter, _report(MessageType.GOAL_STATUS, {
                "task_id": "t", "goal_id": "g", "status": "completed", "summary": "done"})),
        ]
        assert all(r.success for r in receipts) and len(receipts) == 2
        texts = [m["content"] for m in await db_chat.list_messages("s-web", pool=pool) if m["role"] == "assistant"]
        assert "任务「计算器」已分解为 3 个步骤，开始执行。" in texts[0]
        assert texts[1] == "任务「计算器」已完成：done"
        pushed = await asyncio.wait_for(live.__anext__(), 2)
        assert pushed["kind"] == "chat.message" and pushed["payload"]["content"].startswith("任务「计算器」已分解")
        await live.aclose()

        # A page opened after the end gets nothing pushed; a refresh reads the stored messages.
        late = adapter.iter_chat_events("s-web", heartbeat=0.1)
        assert (await late.__anext__())["kind"] == "heartbeat"
        await late.aclose()

    _run(test, monkeypatch)


def test_a_non_final_status_is_a_stream_status_only(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from looper.agents.bus import MessageBus
    from looper.db import chat as db_chat

    async def test(pool: Any) -> None:
        bus = MessageBus(runtime_root=tmp_path)
        adapter = WebChannelAdapter()
        adapter._bus = bus
        manager = ManagerAgent(config=ManagerConfig(work_dir=tmp_path), runtime_root=tmp_path)
        manager._bus = bus
        status = _report(MessageType.GOAL_STATUS, {"task_id": "t", "goal_id": "g", "status": "executing",
                                                   "summary": "half"})
        # No stream: dropped, nothing stored.
        (receipt,) = await _relay(manager, adapter, status)
        assert not receipt.success and "no active SSE stream" in (receipt.error or "")
        # An open stream gets it as a status.
        adapter._sse_queues["c1"] = asyncio.Queue()
        (receipt,) = await _relay(manager, adapter, status)
        assert receipt.success
        assert (await adapter._sse_queues["c1"].get())["kind"] == "agent.phase"
        assert await db_chat.list_messages("s-web", pool=pool) == []

    _run(test, monkeypatch)


def test_a_handed_over_final_report_reaches_the_thread(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from looper.agents.bus import MessageBus
    from looper.db import chat as db_chat

    async def test(pool: Any) -> None:
        bus = MessageBus(runtime_root=tmp_path)
        adapter = WebChannelAdapter()
        adapter._bus = bus
        manager = ManagerAgent(config=ManagerConfig(work_dir=tmp_path), runtime_root=tmp_path)
        manager._bus = bus
        await _relay(manager, adapter, _report(
            MessageType.GOAL_STATUS, {"task_id": "t", "goal_id": "g", "status": "failed", "summary": "boom"},
            handed_over_from="manager-review"))
        texts = [m["content"] for m in await db_chat.list_messages("s-web", pool=pool)]
        assert texts == ["任务「计算器」失败：boom"]

    _run(test, monkeypatch)
