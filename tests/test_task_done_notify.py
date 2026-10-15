"""The task-done notice: when a task asked for in Telegram or Lark completes or fails, the chat it came from is
told once, by the TaskRouter, in the background (``agents.task_notice``).

The TaskRouter sends through its ``sender`` (the server's ``ChannelRegistry.notify`` in production, a recorder
here, or the registry with the stand-in lark-cli and a stand-in Telegram bot end to end). The Manager forwards a
task's progress to its chat but not its end to these channels; the web chat still gets the end from the Manager.
Nothing reaches a real chat or user.
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from lark_cli_stand_in import OPEN_ID
from looper.agents.bus import MessageBus
from looper.agents.channels.allowlist import SenderAllowlist
from looper.agents.channels.base import ChannelAdapter
from looper.agents.channels.registry import ChannelRegistry
from looper.agents.message import (
    MANAGER_ADDRESS,
    TASK_ROUTER_ADDRESS,
    MessageEnvelope,
    MessageType,
    planner_address,
)
from looper.agents.schedule_notify import SNIPPET_CHARS
from looper.agents.task_notice import NotifyChannel, ends_task, notice_text, notify_chat
from looper.agents.tiers.manager import ManagerAgent, ManagerConfig
from looper.agents.tiers.planner import closing_note
from looper.agents.tiers.task import Task, TaskRouter
from tests.test_manager_reply import _answer, _fake_claude, _scripted
from tests.test_planner_agent import WORK_OK, Harness, always, decide, plan, step


class _Sender:
    """Records every (channel_type, chat, text) it is asked to send; answers sent, or a failure when ``ok`` is
    False. With ``hold``, each send waits until the event is set."""

    def __init__(self, *, ok: bool = True, boom: bool = False, hold: asyncio.Event | None = None) -> None:
        self.calls: list[tuple[str, str, str]] = []
        self._ok = ok
        self._boom = boom
        self._hold = hold

    async def __call__(self, channel_type: str, chat: str, text: str) -> tuple[bool, str | None]:
        self.calls.append((channel_type, chat, text))
        if self._hold is not None:
            await self._hold.wait()
        if self._boom:
            raise RuntimeError("the channel exploded")
        return (True, None) if self._ok else (False, "the telegram channel is not running")


def _router(sender: Any, tmp_path: Path) -> TaskRouter:
    router = TaskRouter(runtime_root=tmp_path, sender=sender)
    router._bus = SimpleNamespace(publish=AsyncMock())  # the status is also forwarded to the Manager
    return router


def _status(task_id: str, status: str, summary: str) -> MessageEnvelope:
    return MessageEnvelope.create(
        source=planner_address(task_id), target=TASK_ROUTER_ADDRESS, message_type=MessageType.GOAL_STATUS,
        payload={"task_id": task_id, "status": status, "summary": summary},
    )


def _task(router: TaskRouter, *, channel: str, chat_id: str, task_id: str = "task-n1") -> Task:
    task = Task(task_id=task_id, requirement="ship it", goal_id="g", work_dir="/w", project_id="proj-1",
                origin_channel_id=channel, chat_id=chat_id)
    router._tasks[task.task_id] = task
    return task


async def _ended(router: TaskRouter, envelope: MessageEnvelope) -> None:
    """The router's handling of a Planner's status, then its notices sent."""
    await router._handle_planner_status(envelope)
    await asyncio.gather(*router._notices)


@pytest.mark.usefixtures("agent_db")
class TestTaskDoneNotify:
    def test_completed_task_notifies_its_telegram_chat(self, tmp_path: Path) -> None:
        sender = _Sender()
        router = _router(sender, tmp_path)
        _task(router, channel="channel-telegram", chat_id="1000001")
        asyncio.run(_ended(router, _status("task-n1", "completed", "## Delivered\nall green")))
        assert sender.calls == [("telegram", "1000001", "✅ 任务完成 · ship it\n## Delivered all green\n/#tasks/task-n1")]

    def test_failed_task_says_so_once_with_its_closing_note(self, tmp_path: Path) -> None:
        sender = _Sender()
        router = _router(sender, tmp_path)
        _task(router, channel="channel-telegram", chat_id="-1001234")  # a group chat: its id
        note = closing_note("A step failed and the Planner could not go on.", [("s1", "failed", None)])
        asyncio.run(_ended(router, _status("task-n1", "failed", note)))
        ((channel, chat, text),) = sender.calls
        assert (channel, chat) == ("telegram", "-1001234")
        first, snippet, link = text.split("\n")
        assert first == "❌ 任务失败 · ship it" and text.count("任务失败") == 1
        assert snippet.startswith("## Why it ended A step failed") and link == "/#tasks/task-n1"

    def test_a_lark_task_tells_its_senders_open_id(self, tmp_path: Path) -> None:
        sender = _Sender()
        router = _router(sender, tmp_path)
        _task(router, channel="channel-lark", chat_id=OPEN_ID)
        asyncio.run(_ended(router, _status("task-n1", "completed", "done")))
        assert sender.calls == [("lark", OPEN_ID, "✅ 任务完成 · ship it\ndone\n/#tasks/task-n1")]

    def test_a_long_summary_is_clipped(self) -> None:
        text = notice_text("task-n1", "x" * 100, "completed", "word " * 200)
        first, snippet, link = text.split("\n")
        assert first == f"✅ 任务完成 · {'x' * 40}…" and len(snippet) == SNIPPET_CHARS + 1 and snippet.endswith("…")
        assert notice_text("task-n1", "  ", "failed", None) == "❌ 任务失败 · task-n1\n/#tasks/task-n1"

    @pytest.mark.parametrize(("channel", "chat_id"), [("channel-web", "sess-1"), ("channel-telegram", ""),
                                                      ("", "1000001")])
    def test_a_web_task_or_one_without_a_chat_is_not_told(self, tmp_path: Path, channel: str, chat_id: str) -> None:
        sender = _Sender()
        router = _router(sender, tmp_path)
        _task(router, channel=channel, chat_id=chat_id)
        asyncio.run(_ended(router, _status("task-n1", "completed", "done")))
        assert sender.calls == []

    @pytest.mark.parametrize("sender", [_Sender(boom=True), _Sender(ok=False)])
    def test_a_sending_failure_is_logged_and_breaks_nothing(self, tmp_path: Path, sender: _Sender,
                                                            caplog: pytest.LogCaptureFixture) -> None:
        router = _router(sender, tmp_path)
        task = _task(router, channel="channel-telegram", chat_id="1000001")
        with caplog.at_level(logging.WARNING):
            asyncio.run(_ended(router, _status("task-n1", "failed", "boom")))
        assert len(sender.calls) == 1 and task.status == "failed"
        assert router._bus.publish.await_count == 1  # the status still reached the Manager
        assert "task task-n1: " in caplog.text and "chat" in caplog.text

    def test_no_sender_is_a_no_op(self, tmp_path: Path) -> None:
        router = _router(None, tmp_path)
        _task(router, channel="channel-telegram", chat_id="1000001")
        asyncio.run(_ended(router, _status("task-n1", "completed", "done")))  # must not raise
        assert router._notices == set()


class _Planner:
    """A Planner that starts and stops at once; its task ends when the test says so."""

    def __init__(self, *, task_id: str, **_: Any) -> None:
        self.agent_id = f"planner-{task_id}"
        self.agent_address = planner_address(task_id)

    async def start(self, bus: Any) -> None:
        pass

    async def stop(self) -> None:
        pass


@pytest.mark.usefixtures("agent_db")
def test_a_slow_channel_holds_up_no_routing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from looper.agents.tiers import task as task_module

    monkeypatch.setattr(task_module, "PlannerAgent", _Planner)
    recheck = AsyncMock()

    async def main() -> tuple[list[str], int, list[str], bool]:
        hold = asyncio.Event()
        sender = _Sender(hold=hold)
        bus = MessageBus(runtime_root=tmp_path)
        router = TaskRouter(runtime_root=tmp_path, sender=sender)
        await router.start(bus)
        for task_id, chat in (("task-a", "1000001"), ("task-b", "")):
            await bus.publish(MessageEnvelope.create(
                source=MANAGER_ADDRESS, target=TASK_ROUTER_ADDRESS, message_type=MessageType.TASK_REQUEST,
                payload={"task_id": task_id, "requirement": "ship it", "work_dir": "/w", "project_id": "proj-1",
                         "chat_id": chat}, metadata={"origin_channel_id": "channel-telegram"}))
        await bus.publish(MessageEnvelope.create(source=MANAGER_ADDRESS, target=TASK_ROUTER_ADDRESS,
                                                 message_type=MessageType.TASK_START, payload={"task_id": "task-a"}))
        await _until(lambda: "task-a" in router._planners)
        monkeypatch.setattr(router, "recheck", recheck)
        # task-a ends; its notice hangs. Its Planner is still torn down, the queue rechecked, and task-b starts.
        await bus.publish(_status("task-a", "completed", "done"))
        await bus.publish(MessageEnvelope.create(source=MANAGER_ADDRESS, target=TASK_ROUTER_ADDRESS,
                                                 message_type=MessageType.TASK_START, payload={"task_id": "task-b"}))
        await _until(lambda: "task-a" not in router._planners and router._tasks.get("task-b") is not None)
        while_held = (sorted(router._tasks), recheck.await_count, [call[1] for call in sender.calls],
                      next(iter(router._notices)).done())
        await router.stop()  # cancels the notice still being sent
        assert router._notices == set()
        await bus.shutdown()
        return while_held

    tasks, rechecks, chats, done = asyncio.run(asyncio.wait_for(main(), timeout=20))
    assert tasks == ["task-a", "task-b"] and rechecks >= 1 and chats == ["1000001"] and not done


async def _until(condition: Any, timeout: float = 10.0) -> None:
    deadline = asyncio.get_event_loop().time() + timeout
    while not condition():
        if asyncio.get_event_loop().time() > deadline:
            raise AssertionError("timed out")
        await asyncio.sleep(0.01)


# --- Which chat a task records, and what the Manager forwards ---


@pytest.mark.parametrize(("channel", "sender", "chat", "expected"), [
    ("channel-lark", OPEN_ID, "oc_dm", OPEN_ID),  # Lark's notify sends to a user
    ("channel-telegram", "7", "-1001234", "-1001234"),  # Telegram's to a chat
    ("channel-web", None, "sess-1", None),
    (None, None, None, None),
])
def test_the_chat_a_task_records(channel: str | None, sender: str | None, chat: str | None,
                                 expected: str | None) -> None:
    assert notify_chat(channel, sender=sender, chat=chat) == expected


@pytest.mark.parametrize(("status", "ends"), [
    ("completed", True), ("failed", True),
    # A user's stop or cancel ends a task elsewhere; a Planner never reports them, and nothing else is an end.
    ("stopped", False), ("cancelled", False), ("executing", False), ("stopping", False), ("unknown", False),
    ("", False), (None, False),
])
def test_only_completed_and_failed_end_a_task(status: str | None, ends: bool) -> None:
    assert ends_task(status) is ends


def test_the_notify_channels_are_the_adapters_that_implement_notify(tmp_path: Path) -> None:
    from looper.agents.channels.lark import LarkChannelAdapter
    from looper.agents.channels.telegram import TelegramChannelAdapter
    from looper.agents.channels.web import WebChannelAdapter

    adapters = [WebChannelAdapter(),
                TelegramChannelAdapter(bot_token="0:x", allowlist=SenderAllowlist("channel-telegram", frozenset())),
                LarkChannelAdapter(runtime_root=tmp_path, allowlist=SenderAllowlist("channel-lark", frozenset()))]
    implementing = {adapter.channel_type for adapter in adapters if type(adapter).notify is not ChannelAdapter.notify}
    assert implementing == {channel.value for channel in NotifyChannel}


@pytest.mark.usefixtures("agent_db")
@pytest.mark.parametrize(("origin", "status", "forwarded"), [
    ("channel-web", "completed", True),
    ("channel-web", "failed", True),
    ("channel-telegram", "completed", False),  # the TaskRouter tells this chat
    ("channel-lark", "failed", False),
    ("channel-telegram", "executing", True),  # progress still goes to every chat
    ("channel-lark", "executing", True),
])
def test_the_manager_forwards_a_tasks_end_only_where_it_is_not_told(tmp_path: Path, origin: str, status: str,
                                                                       forwarded: bool) -> None:
    async def main() -> list[str]:
        bus = MessageBus(runtime_root=tmp_path)
        manager = ManagerAgent(config=ManagerConfig(work_dir=tmp_path), runtime_root=tmp_path)
        manager._bus = bus
        await manager._handle(MessageEnvelope.create(
            source=TASK_ROUTER_ADDRESS, target=MANAGER_ADDRESS, message_type=MessageType.GOAL_STATUS,
            payload={"task_id": "t1", "status": status, "summary": "## Delivered\ncalc"}, correlation_id="c1",
            metadata={"origin_channel_id": origin}))
        queue = bus.get_queue(origin)
        texts = [queue.get_nowait().payload["text"] for _ in range(queue.qsize())]
        await bus.shutdown()
        return texts

    assert asyncio.run(main()) == (["## Delivered\ncalc"] if forwarded else [])


# --- End to end: a chat message, the Manager, the TaskRouter, a Planner and a Worker, the notice ---


class _Bot:
    """A stand-in Telegram bot: records what it sends."""

    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []

    async def send_message(self, **kwargs: Any) -> Any:
        self.sent.append(kwargs)
        return SimpleNamespace(message_id=100 + len(self.sent))

    async def edit_message_text(self, **kwargs: Any) -> Any:
        return SimpleNamespace(message_id=kwargs["message_id"])

    async def set_message_reaction(self, **kwargs: Any) -> bool:
        return True


def _tg_update(text: str, message_id: int) -> Any:
    return SimpleNamespace(effective_user=SimpleNamespace(id=7, username="someone"),
                           effective_chat=SimpleNamespace(id=7, type="private"),
                           message=SimpleNamespace(message_id=message_id, text=text))


def _lark_event(text: str, message_id: str) -> dict[str, Any]:
    return {"type": "im.message.receive_v1", "chat_id": "oc_dm", "chat_type": "p2p", "message_id": message_id,
            "message_type": "text", "sender_id": OPEN_ID, "sender_type": "user", "content": text}


def _run_a_task(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, planner_claude: Any, agent_db: Any,
                channel: ChannelAdapter, say: Any) -> dict[str, str]:
    """Ask for a task in ``channel`` (``say(text, n)`` sends its n-th message there), start it, and let it
    complete; each task's id and the chat it recorded, read when it ended."""
    from looper.db import tasks as db_tasks

    claude = planner_claude(plan(step("s1")), decide("accept"))
    manager_claude = _scripted(_answer(project="calc"), json.dumps({"action": "task_start", "task_id": "<task id>"}))
    # The Manager checks that the task it starts is ready.
    monkeypatch.setattr(db_tasks, "get_task", AsyncMock(side_effect=lambda task_id: (
        {**agent_db.store.ready[task_id], "status": db_tasks.READY} if task_id in agent_db.store.ready else None)))

    async def main() -> dict[str, str]:
        h = Harness(tmp_path, claude, always(WORK_OK))
        agent_db.find_project_by_name.return_value = {"id": "proj-1", "name": "calc", "description": "",
                                                       "work_dir": str(h.work_dir)}
        bus = h.bus
        channel._bus = bus
        registry = ChannelRegistry(bus)
        registry._channels[channel.channel_id] = channel  # running, without polling or lark-cli's consumer
        router = TaskRouter(runtime_root=tmp_path, claude_bin=claude.bin, sender=registry.notify)
        await router.start(bus)
        manager = ManagerAgent(config=ManagerConfig(claude_bin=_fake_claude(tmp_path, manager_claude),
                                                    work_dir=tmp_path), runtime_root=tmp_path)
        manager._bus = bus

        async def pump() -> None:
            while True:
                for queue, handle in ((bus.get_queue("manager"), manager._handle),
                                      (bus.get_queue(channel.channel_id), channel.deliver)):
                    while not queue.empty():
                        await handle(queue.get_nowait())
                await asyncio.sleep(0.01)

        pumping = asyncio.ensure_future(pump())
        await say("build calc", 1)
        await h.until(lambda: agent_db.store.ready)
        await say("开始", 2)
        await h.until(lambda: router._tasks and not router._planners and not router._notices
                      and next(iter(router._tasks.values())).status == "completed", timeout=30)
        chats = {task.task_id: task.chat_id for task in router._tasks.values()}
        await h.until(lambda: bus.get_queue("manager").empty() and bus.get_queue(channel.channel_id).empty())
        await asyncio.sleep(0.1)
        pumping.cancel()
        await asyncio.gather(pumping, return_exceptions=True)
        await router.stop()
        await manager.stop()
        await channel._reactions.stop()  # what the channel's stop() does, without its bot or lark-cli consumer
        await bus.shutdown()
        return chats

    return asyncio.run(main())


@pytest.mark.usefixtures("agent_db")
def test_a_lark_task_tells_its_sender_once_when_it_completes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                                             lark_cli_stand_in: Path, planner_claude: Any,
                                                             agent_db: Any) -> None:
    from looper.agents.channels.lark import LarkChannelAdapter

    lark = LarkChannelAdapter(runtime_root=tmp_path, allowlist=SenderAllowlist("channel-lark", frozenset({OPEN_ID})))
    lark._running = True

    async def say(text: str, n: int) -> None:
        await lark._handle_lark_event(_lark_event(text, f"om_in{n}"))

    ((task_id, chat),) = _run_a_task(tmp_path, monkeypatch, planner_claude, agent_db, lark, say).items()
    assert chat == OPEN_ID
    sends = [json.loads(line) for line in lark_cli_stand_in.read_text().splitlines() if "+messages-send" in line]
    told = [argv for argv in sends if "--user-id" in argv]
    assert [argv[argv.index("--user-id") + 1] for argv in told] == [OPEN_ID]
    assert told[0][-2] == "--text" and told[0][-1].startswith("✅ 任务完成 · build it\n## Delivered calc.py")
    assert told[0][-1].endswith(f"/#tasks/{task_id}")
    # The end arrives once; the progress still comes to the chat the task was asked for in.
    assert sum("Delivered" in argv[-1] for argv in sends) == 1
    assert any(argv[-1].startswith("任务已分解为 1 个步骤") for argv in sends if "--chat-id" in argv)


@pytest.mark.usefixtures("agent_db")
def test_a_telegram_task_tells_its_chat_once_when_it_completes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                                              planner_claude: Any, agent_db: Any) -> None:
    pytest.importorskip("telegram")
    from looper.agents.channels.telegram import TelegramChannelAdapter

    bot = _Bot()
    telegram = TelegramChannelAdapter(bot_token="0:x", allowlist=SenderAllowlist("channel-telegram", frozenset({"7"})))
    telegram._app = SimpleNamespace(bot=bot)

    async def say(text: str, n: int) -> None:
        await telegram._on_message(_tg_update(text, n), None)

    ((task_id, chat),) = _run_a_task(tmp_path, monkeypatch, planner_claude, agent_db, telegram, say).items()
    assert chat == "7"
    ends = [sent for sent in bot.sent if "Delivered" in sent["text"]]
    assert len(ends) == 1 and ends[0]["chat_id"] == 7 and ends[0]["text"].startswith("✅ 任务完成 · build it\n")
    assert ends[0]["text"].endswith(f"/#tasks/{task_id}")
    assert any(sent["text"].startswith("任务已分解为 1 个步骤") for sent in bot.sent)
