"""The task-done notice: when a task completes or fails, the chat it came from is told, in one line.

The TaskRouter pushes the notice through its ``sender`` (the server's ``ChannelRegistry.notify`` in production, a
recorder here) for Telegram only (Lark is a follow-up). Web is skipped (the dashboard shows it), an empty chat is skipped, and a
sender that fails never breaks task teardown.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from looper.agents.message import (
    TASK_ROUTER_ADDRESS,
    MessageEnvelope,
    MessageType,
    planner_address,
)
from looper.agents.tiers.task import Task, TaskRouter


class _Sender:
    """Records every (channel_type, chat, text) it is asked to send; answers sent, or a failure when ``ok`` is
    False."""

    def __init__(self, *, ok: bool = True, boom: bool = False) -> None:
        self.calls: list[tuple[str, str, str]] = []
        self._ok = ok
        self._boom = boom

    async def __call__(self, channel_type: str, chat: str, text: str) -> tuple[bool, str | None]:
        self.calls.append((channel_type, chat, text))
        if self._boom:
            raise RuntimeError("the channel exploded")
        return (True, None) if self._ok else (False, "the telegram channel is not running")


def _router(sender: Any) -> TaskRouter:
    router = TaskRouter(runtime_root=Path("/tmp"), sender=sender)
    router._bus = SimpleNamespace(publish=AsyncMock())  # the status is also forwarded to the Manager
    return router


def _status(task_id: str, status: str, summary: str) -> MessageEnvelope:
    return MessageEnvelope.create(
        source=planner_address(task_id), target=TASK_ROUTER_ADDRESS, message_type=MessageType.GOAL_STATUS,
        payload={"task_id": task_id, "status": status, "summary": summary},
    )


def _task(router: TaskRouter, *, channel: str, chat_id: str) -> Task:
    task = Task(task_id="task-n1", requirement="ship it", goal_id="g", work_dir="/tmp", project_id="proj-1",
                origin_channel_id=channel, chat_id=chat_id)
    router._tasks[task.task_id] = task
    return task


@pytest.mark.usefixtures("agent_db")
class TestTaskDoneNotify:
    def test_completed_task_notifies_its_telegram_chat(self) -> None:
        async def _run() -> None:
            sender = _Sender()
            router = _router(sender)
            _task(router, channel="channel-telegram", chat_id="1000001")
            await router._handle_planner_status(_status("task-n1", "completed", "all green"))
            assert sender.calls == [("telegram", "1000001", "✅ 任务完成：all green")]

        asyncio.run(_run())

    def test_failed_task_notifies_its_telegram_chat(self) -> None:
        async def _run() -> None:
            sender = _Sender()
            router = _router(sender)
            _task(router, channel="channel-telegram", chat_id="1000001")
            await router._handle_planner_status(_status("task-n1", "failed", "boom"))
            assert sender.calls == [("telegram", "1000001", "❌ 任务失败：boom")]

        asyncio.run(_run())

    def test_lark_task_is_not_notified_yet(self) -> None:
        # A Lark task stores a conversation chat id (oc_...) but notify sends to a user open id (ou_...);
        # a chat-targeted Lark send is a follow-up, so Lark is skipped for now.
        async def _run() -> None:
            sender = _Sender()
            router = _router(sender)
            _task(router, channel="channel-lark", chat_id="oc_abcdefgh12")
            await router._handle_planner_status(_status("task-n1", "completed", "done"))
            assert sender.calls == []

        asyncio.run(_run())

    def test_web_task_is_not_notified(self) -> None:
        async def _run() -> None:
            sender = _Sender()
            router = _router(sender)
            _task(router, channel="channel-web", chat_id="sess-1")
            await router._handle_planner_status(_status("task-n1", "completed", "done"))
            assert sender.calls == []

        asyncio.run(_run())

    def test_a_task_without_a_chat_is_not_notified(self) -> None:
        async def _run() -> None:
            sender = _Sender()
            router = _router(sender)
            _task(router, channel="channel-telegram", chat_id="")
            await router._handle_planner_status(_status("task-n1", "completed", "done"))
            assert sender.calls == []

        asyncio.run(_run())

    def test_a_sending_failure_does_not_break_teardown(self) -> None:
        async def _run() -> None:
            sender = _Sender(boom=True)
            router = _router(sender)
            _task(router, channel="channel-telegram", chat_id="1000001")
            # The channel raised; the status still reaches the Manager and teardown finishes.
            await router._handle_planner_status(_status("task-n1", "failed", "boom"))
            assert sender.calls == [("telegram", "1000001", "❌ 任务失败：boom")]
            assert router._bus.publish.await_count == 1

        asyncio.run(_run())

    def test_no_sender_is_a_no_op(self) -> None:
        async def _run() -> None:
            router = _router(None)
            _task(router, channel="channel-telegram", chat_id="1000001")
            await router._handle_planner_status(_status("task-n1", "completed", "done"))  # must not raise

        asyncio.run(_run())
