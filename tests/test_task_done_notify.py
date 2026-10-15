"""The task-done notice: when a task asked for in Telegram or Lark completes or fails, the chat it came from is
told once, as the task's last message there (``agents.task_notice``).

The TaskRouter adds the notice to the final status it passes to the Manager; the Manager sends it to the channel
instead of the final summary, on the same path as the task's progress; the channel sends it like an answer
(Markdown), to who the task recorded. End to end the channels run with the stand-in lark-cli and a stand-in
Telegram bot. Nothing reaches a real chat or user.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from lark_cli_stand_in import OPEN_ID
from looper.agents.bus import MessageBus
from looper.agents.channels.allowlist import SenderAllowlist
from looper.agents.channels.base import ChannelAdapter
from looper.agents.message import (
    MANAGER_ADDRESS,
    TASK_ROUTER_ADDRESS,
    MessageEnvelope,
    MessageType,
    channel_address,
    planner_address,
)
from looper.agents.task_notice import LEAD_CHARS, Notice, NotifyChannel, ends_task, lead, notice_text, notify_chat
from looper.agents.tiers.manager import ManagerAgent, ManagerConfig
from looper.agents.tiers.planner import closing_note
from looper.agents.tiers.task import Task, TaskRouter
from tests.test_manager_reply import _answer, _fake_claude, _project, _scripted, _task_chat
from tests.test_planner_agent import FINAL_RESULT, WORK_OK, Harness, always, decide, plan, step

DASHBOARD = "http://looper.test:8767"


def _router(tmp_path: Path) -> TaskRouter:
    router = TaskRouter(runtime_root=tmp_path, dashboard_url=DASHBOARD)
    router._bus = SimpleNamespace(publish=AsyncMock())  # what it passes on to the Manager
    return router


def _status(task_id: str, status: str, summary: str) -> MessageEnvelope:
    return MessageEnvelope.create(
        source=planner_address(task_id), target=TASK_ROUTER_ADDRESS, message_type=MessageType.GOAL_STATUS,
        payload={"task_id": task_id, "status": status, "summary": summary},
    )


def _task(router: TaskRouter, *, channel: str, chat_id: str, said: str = "帮我给 calc 加上除法") -> Task:
    task = Task(task_id="task-n1", requirement="Add division to calc.", goal_id="g", work_dir="/w",
                project_id="proj-1", origin_channel_id=channel, chat_id=chat_id, original_user_text=said)
    router._tasks[task.task_id] = task
    return task


def _passed_on(router: TaskRouter, envelope: MessageEnvelope) -> list[dict[str, Any]]:
    """The payloads of the statuses the router passed on to the Manager for ``envelope``, and after."""
    asyncio.run(router._handle_planner_status(envelope))
    return [call.args[0].payload for call in router._bus.publish.await_args_list]


@pytest.mark.usefixtures("agent_db")
class TestTheNotice:
    def test_a_completed_telegram_task(self, tmp_path: Path) -> None:
        router = _router(tmp_path)
        _task(router, channel="channel-telegram", chat_id="1000001")
        (passed,) = _passed_on(router, _status("task-n1", "completed", FINAL_RESULT))
        assert Notice.from_payload(passed["notice"]) == Notice(NotifyChannel.TELEGRAM, "1000001", (
            "✅ 任务完成 · 帮我给 calc 加上除法\n## Delivered\ncalc.py with add and its tests\n"
            f"{DASHBOARD}/#tasks/task-n1"))

    def test_a_failed_lark_task_says_why_once_with_a_markdown_link(self, tmp_path: Path) -> None:
        router = _router(tmp_path)
        _task(router, channel="channel-lark", chat_id=OPEN_ID)
        note = closing_note("A step failed and the Planner could not go on.", [("s1", "failed", None)])
        (passed,) = _passed_on(router, _status("task-n1", "failed", note))
        notice = Notice.from_payload(passed["notice"])
        assert notice is not None and (notice.channel, notice.chat) == (NotifyChannel.LARK, OPEN_ID)
        link = f"{DASHBOARD}/#tasks/task-n1"
        assert notice.text == ("❌ 任务失败 · 帮我给 calc 加上除法\n## Why it ended\n"
                               f"A step failed and the Planner could not go on.\n[{link}]({link})")
        assert notice.text.count("任务失败") == 1 and "## Done" not in notice.text

    @pytest.mark.parametrize(("said", "name"), [
        ("帮我给 calc 加上除法", "帮我给 calc 加上除法"),
        ("", "Add division to calc."),  # no text of the user's: the requirement
        ("给计算器加上除法，" * 6, f"{'给计算器加上除法，' * 6}"[:40] + "…"),
    ])
    def test_the_name_is_what_the_user_wrote(self, tmp_path: Path, said: str, name: str) -> None:
        router = _router(tmp_path)
        _task(router, channel="channel-telegram", chat_id="1000001", said=said)
        (passed,) = _passed_on(router, _status("task-n1", "completed", "done"))
        assert passed["notice"]["text"].split("\n")[0] == f"✅ 任务完成 · {name}"

    @pytest.mark.parametrize(("channel", "chat_id"), [("channel-web", "sess-1"), ("channel-telegram", ""),
                                                      ("", "1000001")])
    def test_a_web_task_or_one_without_a_chat_has_no_notice(self, tmp_path: Path, channel: str,
                                                            chat_id: str) -> None:
        router = _router(tmp_path)
        _task(router, channel=channel, chat_id=chat_id)
        (passed,) = _passed_on(router, _status("task-n1", "completed", "done"))
        assert "notice" not in passed

    def test_progress_has_no_notice_and_nothing_comes_after_the_end(self, tmp_path: Path) -> None:
        router = _router(tmp_path)
        _task(router, channel="channel-telegram", chat_id="1000001")
        (progress,) = _passed_on(router, _status("task-n1", "executing", "进度更新: 0/1 完成"))
        assert "notice" not in progress
        asyncio.run(router._handle_planner_status(_status("task-n1", "completed", "done")))
        passed = _passed_on(router, _status("task-n1", "executing", "进度更新: 1/1 完成"))
        assert len(passed) == 2 and "notice" in passed[-1]  # the late progress is not passed on


@pytest.mark.parametrize(("summary", "shown"), [
    (FINAL_RESULT, "## Delivered\ncalc.py with add and its tests"),
    ("line one\nline two", "line one\nline two"),  # no sections: the start, its line breaks kept
    (None, ""),
])
def test_the_summary_shows_its_first_section(summary: str | None, shown: str) -> None:
    assert lead(summary) == shown


def test_a_long_first_section_is_clipped_with_its_line_breaks() -> None:
    text = lead("## Delivered\n" + "- one more thing\n" * 40)
    assert len(text) == LEAD_CHARS + 1 and text.endswith("…") and text.count("\n") > 10


def test_the_notice_links_relative_without_a_dashboard_address() -> None:
    text = notice_text("task-n1", "", "failed", None, channel=NotifyChannel.TELEGRAM)
    assert text == "❌ 任务失败 · task-n1\n/#tasks/task-n1"


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


# --- The Manager: the notice instead of the final summary, on the progress' path ---


_NOTICE = Notice(NotifyChannel.TELEGRAM, "1000001", "✅ 任务完成 · x\n/#tasks/t1")


@pytest.mark.usefixtures("agent_db")
@pytest.mark.parametrize(("origin", "status", "notice", "sent"), [
    ("channel-telegram", "completed", _NOTICE, [("channel-telegram", MessageType.CHAT_NOTICE)]),
    ("channel-web", "completed", None, [("channel-web", MessageType.STATUS_UPDATE)]),
    ("channel-web", "failed", None, [("channel-web", MessageType.STATUS_UPDATE)]),
    # A Telegram or Lark task that recorded no chat: its end comes from the Manager, as before the notice.
    ("channel-lark", "failed", None, [("channel-lark", MessageType.STATUS_UPDATE)]),
    ("channel-telegram", "executing", None, [("channel-telegram", MessageType.STATUS_UPDATE)]),
    # The notice carries its own channel: a status without the Planner's metadata still tells the chat.
    (None, "failed", _NOTICE, [("channel-telegram", MessageType.CHAT_NOTICE)]),
])
def test_the_manager_sends_the_notice_instead_of_the_end(tmp_path: Path, origin: str | None, status: str,
                                                         notice: Notice | None,
                                                         sent: list[tuple[str, MessageType]]) -> None:
    async def main() -> list[tuple[str, MessageType, dict[str, Any]]]:
        bus = MessageBus(runtime_root=tmp_path)
        manager = ManagerAgent(config=ManagerConfig(work_dir=tmp_path), runtime_root=tmp_path)
        manager._bus = bus
        payload = {"task_id": "t1", "status": status, "summary": "## Delivered\ncalc",
                   **({"notice": notice.payload()} if notice else {})}
        await manager._handle(MessageEnvelope.create(
            source=TASK_ROUTER_ADDRESS, target=MANAGER_ADDRESS, message_type=MessageType.GOAL_STATUS,
            payload=payload, correlation_id="c1", metadata={"origin_channel_id": origin} if origin else {}))
        out = []
        for queue_name in ("channel-telegram", "channel-lark", "channel-web"):
            queue = bus.get_queue(queue_name)
            out += [(queue_name, e.message_type, e.payload) for e in (queue.get_nowait() for _ in range(queue.qsize()))]
        await bus.shutdown()
        return out

    out = asyncio.run(main())
    assert [(queue, kind) for queue, kind, _ in out] == sent
    if notice:
        assert out[0][2] == notice.payload()


@pytest.mark.usefixtures("agent_db")
def test_a_task_asked_about_first_is_named_by_the_message_that_asked_for_it(agent_db: Any) -> None:
    agent_db.find_project_by_name.return_value = _project("tetris", "/srv/tetris")
    # No project named: Looper asks which one; the answer's task JSON then creates the task.
    chat = _task_chat([_answer(), _answer(project="tetris")], ["帮我写个俄罗斯方块", "tetris"])
    (request,) = chat.requests
    assert request.payload["original_user_text"] == "帮我写个俄罗斯方块"


# --- The channels: the notice like an answer, to who the task recorded ---


def test_lark_sends_the_notice_as_markdown_to_the_user(tmp_path: Path, lark_cli_stand_in: Path) -> None:
    from looper.agents.channels.lark import LarkChannelAdapter

    lark = LarkChannelAdapter(profile="other", runtime_root=tmp_path,
                              allowlist=SenderAllowlist("channel-lark", frozenset({OPEN_ID})))
    notice = Notice(NotifyChannel.LARK, OPEN_ID, "✅ 任务完成 · x\n## Delivered\ncalc")
    receipt = asyncio.run(lark.deliver(MessageEnvelope.create(
        source=MANAGER_ADDRESS, target=channel_address("lark"), message_type=MessageType.CHAT_NOTICE,
        payload=notice.payload(), correlation_id="never-seen")))
    assert receipt.success
    (argv,) = [json.loads(line) for line in lark_cli_stand_in.read_text().splitlines()]
    assert argv == ["--profile", "other", "im", "+messages-send", "--as", "bot", "--user-id", OPEN_ID,
                    "--markdown", notice.text]


def test_telegram_sends_the_notice_like_an_answer() -> None:
    pytest.importorskip("telegram")
    from looper.agents.channels.telegram import TelegramChannelAdapter

    bot = _Bot()
    telegram = TelegramChannelAdapter(bot_token="0:x", allowlist=SenderAllowlist("channel-telegram", frozenset({"7"})))
    telegram._app = SimpleNamespace(bot=bot)
    receipt = asyncio.run(telegram.deliver(MessageEnvelope.create(
        source=MANAGER_ADDRESS, target=channel_address("telegram"), message_type=MessageType.CHAT_NOTICE,
        payload=_NOTICE.payload(), correlation_id="never-seen")))
    assert receipt.success
    ((sent),) = bot.sent
    assert (sent["chat_id"], sent["text"]) == (1000001, _NOTICE.text) and sent["parse_mode"] == "MarkdownV2"


# --- End to end: a chat message, the Manager, the TaskRouter, a Planner and a Worker, the notice ---


class _Bot:
    """A stand-in Telegram bot: records what it sends, a progress message slowly."""

    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []

    async def send_message(self, **kwargs: Any) -> Any:
        if str(kwargs.get("text", "")).startswith("进度更新"):
            await asyncio.sleep(0.5)
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
    complete, the channel delivering on its own outbound loop; each task's id and the chat it recorded."""
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
        channel._running = True
        outbound = asyncio.ensure_future(channel._outbound_loop())  # what start() runs, without polling
        router = TaskRouter(runtime_root=tmp_path, claude_bin=claude.bin, dashboard_url=DASHBOARD)
        await router.start(bus)
        manager = ManagerAgent(config=ManagerConfig(claude_bin=_fake_claude(tmp_path, manager_claude),
                                                    work_dir=tmp_path), runtime_root=tmp_path)
        manager._bus = bus

        async def pump() -> None:  # the Manager's own loop, without the rest of its start()
            queue = bus.get_queue("manager")
            while True:
                await manager._handle(await queue.get())

        pumping = asyncio.ensure_future(pump())
        await say("build calc", 1)
        await h.until(lambda: agent_db.store.ready)
        await say("开始", 2)
        await h.until(lambda: router._tasks and not router._planners
                      and next(iter(router._tasks.values())).status == "completed", timeout=30)
        chats = {task.task_id: task.chat_id for task in router._tasks.values()}
        await h.until(lambda: bus.get_queue("manager").empty() and bus.get_queue(channel.channel_id).empty())
        await asyncio.sleep(1.0)  # a slow progress send under way finishes
        for running in (pumping, outbound):
            running.cancel()
        await asyncio.gather(pumping, outbound, return_exceptions=True)
        await router.stop()
        await manager.stop()
        await channel._reactions.stop()  # what the channel's stop() does, without its bot or lark-cli consumer
        await bus.shutdown()
        return chats

    return asyncio.run(main())


@pytest.mark.usefixtures("agent_db")
def test_a_lark_task_ends_with_one_markdown_notice_after_its_progress(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, lark_cli_stand_in: Path, planner_claude: Any, agent_db: Any,
) -> None:
    from looper.agents.channels.lark import LarkChannelAdapter

    real_send = LarkChannelAdapter._send_message

    async def slow_progress(self: Any, chat_id: str, text: str) -> Any:
        if text.startswith("进度更新"):
            await asyncio.sleep(0.5)
        return await real_send(self, chat_id, text)

    monkeypatch.setattr(LarkChannelAdapter, "_send_message", slow_progress)
    lark = LarkChannelAdapter(runtime_root=tmp_path, allowlist=SenderAllowlist("channel-lark", frozenset({OPEN_ID})))

    async def say(text: str, n: int) -> None:
        await lark._handle_lark_event(_lark_event(text, f"om_in{n}"))

    ((task_id, chat),) = _run_a_task(tmp_path, monkeypatch, planner_claude, agent_db, lark, say).items()
    assert chat == OPEN_ID
    sends = [json.loads(line) for line in lark_cli_stand_in.read_text().splitlines() if "+messages-send" in line]
    link = f"{DASHBOARD}/#tasks/{task_id}"
    # The notice is the task's last message, after every progress message, sent once and as Markdown.
    assert sends[-1][-4:] == ["--user-id", OPEN_ID, "--markdown",
                              f"✅ 任务完成 · build calc\n## Delivered\ncalc.py with add and its tests\n[{link}]({link})"]
    progress = [i for i, argv in enumerate(sends) if argv[-1].startswith("进度更新")]
    assert progress and all(argv[-1] != sends[-1][-1] for argv in sends[:-1])
    assert any(argv[-1].startswith("任务已分解为 1 个步骤") for argv in sends if "--chat-id" in argv)
    assert sum("Delivered" in argv[-1] for argv in sends) == 1


@pytest.mark.usefixtures("agent_db")
def test_a_telegram_task_ends_with_one_notice_after_its_progress(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                                                 planner_claude: Any, agent_db: Any) -> None:
    pytest.importorskip("telegram")
    from looper.agents.channels.telegram import TelegramChannelAdapter

    bot = _Bot()  # sends a progress message slowly
    telegram = TelegramChannelAdapter(bot_token="0:x", allowlist=SenderAllowlist("channel-telegram", frozenset({"7"})))
    telegram._app = SimpleNamespace(bot=bot)

    async def say(text: str, n: int) -> None:
        await telegram._on_message(_tg_update(text, n), None)

    ((task_id, chat),) = _run_a_task(tmp_path, monkeypatch, planner_claude, agent_db, telegram, say).items()
    assert chat == "7"
    texts = [sent["text"] for sent in bot.sent]
    assert texts[-1] == f"✅ 任务完成 · build calc\n## Delivered\ncalc.py with add and its tests\n{DASHBOARD}/#tasks/{task_id}"
    assert bot.sent[-1]["chat_id"] == 7 and bot.sent[-1]["parse_mode"] == "MarkdownV2"
    assert any(text.startswith("进度更新") for text in texts[:-1])
    assert sum("Delivered" in text for text in texts) == 1
