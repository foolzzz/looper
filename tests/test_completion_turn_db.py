"""The Manager's completion turn (``manager.completion_turns``): a task's end in a direct chat or the dashboard chat
is told by the Manager in a read-only turn, with the task's link code adds, and every failure falls back to the
code notice exactly as recorded.

The Manager runs on the api runtime against a loopback model API (``tests/fake_model_api.py``) with its real ops and
a real PostgreSQL; it runs only when LOOPER_TEST_DATABASE_URL points at a disposable database.
"""

from __future__ import annotations

import asyncio
import json
import re
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from looper.agents import chat_settings, completion_events
from looper.agents.claude_cli import ResidentTurn
from looper.agents.message import (MANAGER_ADDRESS, TASK_ROUTER_ADDRESS, MessageEnvelope, MessageType,
                                   channel_address)
from looper.agents.ops import Turn, TurnOrigin
from looper.agents.run_usage import Usage
from looper.agents.tiers.turn_token import TurnStopped
from looper.agents.task_notice import Notice, NotifyChannel, NotifyTarget, TargetKind, web_end_text
from looper.db import projects, tasks
from tests.api_manager_support import USAGE, _manager
from tests.db_support import DSN, run_db, until
from tests.fake_model_api import ResponseScript, call_events, completed, call_item, text_events

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")

THREAD = "chat-session-1"
OPEN_ID = "ou_abcdefgh12"
DM_CHAT = "oc_dm0001"
REPLY = "calc 已经交付：加法和减法都通过了测试。\n要我再加上乘法吗？"


@pytest.fixture
def turns_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(chat_settings, "_completion_turns", chat_settings.CompletionTurns.DM_ONLY)


def _text(answer: str) -> ResponseScript:
    return ResponseScript(events=text_events(answer, usage=USAGE))


def _call(name: str, arguments: dict[str, Any], call_id: str) -> ResponseScript:
    raw = json.dumps(arguments)
    events = call_events(raw, name=name, call_id=call_id, id=f"fc_{call_id}")
    events[-1] = completed([call_item(raw, name=name, call_id=call_id, id=f"fc_{call_id}")], usage=USAGE)
    return ResponseScript(events=events)


async def _task(pool: Any, root: Path, *, manager_id: str = "manager", **columns: Any) -> None:
    project = await projects.create_project(name="calc", work_dir=str(root), pool=pool)
    await tasks.create_task(task_id="t1", requirement="r", project_id=project["id"], work_dir=str(root),
                            status="completed", title="calc", pool=pool, manager_id=manager_id, **columns)


def _goal_status(status: str = "completed", *, thread: str | None = THREAD, notice: Notice | None = None,
                 **metadata: Any) -> MessageEnvelope:
    return MessageEnvelope.create(
        source=TASK_ROUTER_ADDRESS, target=MANAGER_ADDRESS, message_type=MessageType.GOAL_STATUS,
        payload={"task_id": "t1", "goal_id": "g1", "status": status, "summary": "## Done\nadd and sub pass",
                 **({"notice": notice.payload()} if notice else {})},
        correlation_id="c1",
        metadata={"task_title": "calc", **({"origin_thread": thread} if thread else {}),
                  **({"origin_channel_id": "channel-web"} if thread else {}), **metadata})


async def _operator_is_the_lark_person(pool: Any) -> None:
    """The bot names the person as its operator: only an operator's direct chat gets a main-session reply."""
    await pool.execute(
        "INSERT INTO channel_configs (channel_id, channel_type, enabled, config) VALUES ('channel-lark', 'lark', "
        "TRUE, $1::jsonb) ON CONFLICT (channel_id) DO UPDATE SET config = EXCLUDED.config",
        json.dumps({"operator_sender_id": OPEN_ID}))


def _dm_notice() -> Notice:
    return Notice(NotifyChannel.LARK, NotifyTarget(TargetKind.USER, OPEN_ID), "✅ 任务完成 · calc\nadd and sub pass",
                  channel_id="channel-lark")


def _drain(bus: Any, agent_id: str) -> list[MessageEnvelope]:
    queue = bus.get_queue(agent_id)
    return [queue.get_nowait() for _ in range(queue.qsize())]


async def _events(pool: Any) -> list[dict[str, Any]]:
    return [dict(row) for row in await pool.fetch("SELECT * FROM manager_events ORDER BY id")]


def _service(bus: Any, *, running: Any = lambda manager_id: True) -> Any:
    from looper.agents.completion_service import CompletionService

    return CompletionService(bus=bus, manager_running=running, running_managers=lambda: ["manager"])


async def _due(bus: Any, *, after: float = 61.0, service: Any = None) -> list[MessageEnvelope]:
    """The wake-ups the completion service queues ``after`` seconds from now (past the gathering window)."""
    await (service or _service(bus)).run_once(now=datetime.now(timezone.utc) + timedelta(seconds=after))
    return [e for e in _drain(bus, "manager") if e.message_type is MessageType.COMPLETION_DUE]


async def _end_and_tell(chat: Any, envelope: MessageEnvelope) -> None:
    """The task's end reaches the Manager, then the wake-up the completion service queues once its window closed."""
    await chat.manager._handle(envelope)
    (due,) = await _due(chat.bus)
    await chat.manager._handle(due)


def _input(request: Any) -> str:
    return json.dumps(request.body["input"], ensure_ascii=False)


def test_a_turn_answers_no_user_by_its_typed_origin() -> None:
    assert Turn("web", "s", message_id="m1").origin is TurnOrigin.USER
    assert not Turn("web", "s", message_id="m1").autonomous
    assert Turn(None, None).origin is TurnOrigin.OWN and Turn(None, None).autonomous
    assert Turn("web", "s", origin=TurnOrigin.COMPLETION).autonomous


def test_the_dashboard_chat_gets_the_managers_message_and_the_tasks_link_instead_of_the_notice(
        monkeypatch, tmp_path, turns_on):
    async def run(pool):
        async with _manager(pool, tmp_path, monkeypatch, [_text(REPLY)]) as chat:
            await _task(pool, tmp_path, origin_thread=THREAD)
            await chat.manager._handle(_goal_status())
            assert [e.message_type for e in _drain(chat.bus, "channel-web")] == []  # no code notice
            (row,) = await _events(pool)
            assert (row["state"], row["reason"], row["due_at"] is not None) == ("pending", None, True)
            assert _drain(chat.bus, "manager") == [] and await _due(chat.bus, after=59) == []  # the window is open
            (due,) = await _due(chat.bus)
            assert due.payload == {"manager_id": "manager", "channel_id": "channel-web", "chat": THREAD}
            await chat.manager._handle(due)
            (reply,) = _drain(chat.bus, "channel-web")
            assert reply.message_type is MessageType.AGENT_REPLY
            assert reply.payload == {"text": f"{REPLY}\n\n[/#tasks/t1](/#tasks/t1)", "chat_session_id": THREAD}
            (row,) = await _events(pool)
            assert (row["state"], row["reason"]) == ("delivered", None)
            assert row["sent_at"] is not None and row["batch"] is not None and row["record_id"] is not None
            record = await pool.fetchrow("SELECT * FROM llm_records WHERE id = $1", row["record_id"])
            assert record["agent_id"] == "manager" and record["task_id"] is None  # the Manager's cost
            assert json.loads(record["metadata"])["completion"] == {
                "batch": str(row["batch"]), "event_ids": [row["id"]], "task_ids": ["t1"]}
            (request,) = chat.model.requests
            sent = _input(request)
            assert "[COMPLETION] Looper reports tasks that ended." in sent
            assert "Channel: the Looper dashboard chat" in sent
            assert 't1 \\"calc\\" (project calc): completed' in sent and "add and sub pass" in sent
            assert "Projects:" not in sent  # none of the sections before a user's message
            assert await pool.fetchval("SELECT count(*) FROM chat_messages WHERE role = 'user'") == 0

    run_db(run, monkeypatch, timeout=60)


def test_a_lark_direct_chat_gets_the_managers_message_as_the_notice_would_go(monkeypatch, tmp_path, turns_on):
    async def run(pool):
        async with _manager(pool, tmp_path, monkeypatch, [_text(REPLY)]) as chat:
            await _operator_is_the_lark_person(pool)
            await _task(pool, tmp_path)
            await _end_and_tell(chat, _goal_status(thread=None, notice=_dm_notice(),
                                                   reply_route={"chat_id": DM_CHAT},
                                                   origin_channel_id="channel-lark"))
            (sent,) = _drain(chat.bus, "channel-lark")
            assert sent.message_type is MessageType.CHAT_NOTICE
            assert sent.payload == {**_dm_notice().payload(), "text": f"{REPLY}\n\n[/#tasks/t1](/#tasks/t1)"}
            record = sent.metadata["chat_record"]
            assert record["session_id"] == f"lark:{DM_CHAT}" and record["message_id"].startswith("completion-")
            (row,) = await _events(pool)
            assert (row["target_kind"], row["state"]) == ("dm", "delivered")
            assert "Channel: a Lark direct message" in _input(chat.model.requests[0])

    run_db(run, monkeypatch, timeout=60)


def test_the_turns_writes_answer_needs_user_turn_and_confirm_takes_nothing(monkeypatch, tmp_path, turns_on):
    scripts = [_call("looper_task_control", {"action": "restart", "task_id": "t1"}, "call_1"),
               _call("looper_confirm", {"confirmation_id": "c-000000000001"}, "call_2"), _text(REPLY)]

    async def run(pool):
        async with _manager(pool, tmp_path, monkeypatch, scripts) as chat:
            await _task(pool, tmp_path, origin_thread=THREAD)
            waiting = SimpleNamespace(id="c-000000000001", answer_turn=None, at=chat.ops._clock(), asked_in="m0")
            key = ("web", "channel-web", THREAD)
            chat.ops._pending[key] = waiting  # an operation the user was asked to confirm in this chat
            await _end_and_tell(chat, _goal_status())
            actions = await pool.fetch("SELECT tool, status, error_code, message_id FROM manager_actions ORDER BY id")
            assert [(a["tool"], a["status"], a["error_code"], a["message_id"]) for a in actions] == [
                ("looper_task_control", "error", "needs_user_turn", ""),
                ("looper_confirm", "error", "needs_user_turn", "")]
            assert chat.ops._pending[key] is waiting and waiting.answer_turn is None  # not consumed
            assert (await tasks.get_task("t1", pool=pool))["status"] == "completed"  # not restarted
            ((_, reply),) = [(e.message_type, e) for e in _drain(chat.bus, "channel-web")]
            assert reply.payload["text"].startswith(REPLY)
            assert (await _events(pool))[0]["state"] == "delivered"

    run_db(run, monkeypatch, timeout=60)


@pytest.mark.parametrize("case,reason", [("empty", "empty_reply"), ("error", "turn_failed"),
                                         ("timeout", "turn_timeout"), ("stop", "turn_stopped")])
def test_a_turn_that_does_not_reply_falls_back_to_the_notice_as_recorded(monkeypatch, tmp_path, turns_on, case,
                                                                        reason):
    gate = asyncio.Event()
    script = {"empty": _text(""), "error": ResponseScript(status=400, error={"message": "bad request"}),
              "timeout": ResponseScript(events=text_events("late", usage=USAGE), gate=gate),
              "stop": ResponseScript(events=text_events("late", usage=USAGE), gate=gate)}[case]
    if case == "timeout":
        monkeypatch.setattr(completion_events, "TURN_SECONDS", 1.0)

    async def run(pool):
        async with _manager(pool, tmp_path, monkeypatch, [script]) as chat:
            await _task(pool, tmp_path, origin_thread=THREAD)
            await chat.manager._handle(_goal_status())
            (due,) = await _due(chat.bus)
            telling = asyncio.create_task(chat.manager._handle(due))
            if case == "stop":
                await asyncio.wait_for(script.gate_reached.wait(), 20)
                (token,) = chat.manager.live_for_chat("channel-web", THREAD)  # the chat's live turn
                assert (await chat.manager.stop_turn(token.message_id, via="web")).value == "stopping"
            await asyncio.wait_for(telling, 30)
            gate.set()
            (notice,) = _drain(chat.bus, "channel-web")
            assert notice.payload == {"text": web_end_text("completed", "calc", "## Done\nadd and sub pass"),
                                      "chat_session_id": THREAD}
            assert notice.correlation_id == "c1"
            (row,) = await _events(pool)
            assert (row["state"], row["reason"]) == ("fallback", reason)
            assert row["sent_at"] is not None and row["finished_at"] is not None
            assert chat.manager.live_for_chat("channel-web", THREAD) == []
            assert not chat.manager.busy

    run_db(run, monkeypatch, timeout=90)


def test_a_reply_the_channel_cannot_take_falls_back_to_the_notice(monkeypatch, tmp_path, turns_on):
    async def run(pool):
        async with _manager(pool, tmp_path, monkeypatch, [_text(REPLY)]) as chat:
            await _task(pool, tmp_path, origin_thread=THREAD)

            async def refused(*args: Any, **kwargs: Any) -> None:
                raise OSError("the channel is gone")

            monkeypatch.setattr(completion_events, "send_reply", refused)
            await _end_and_tell(chat, _goal_status())
            (notice,) = _drain(chat.bus, "channel-web")
            assert notice.payload["text"] == web_end_text("completed", "calc", "## Done\nadd and sub pass")
            (row,) = await _events(pool)
            assert (row["state"], row["reason"], row["record_id"] is not None) == ("fallback", "send_failed", True)

    run_db(run, monkeypatch, timeout=60)


def test_a_manager_turned_off_or_a_switch_turned_off_before_the_turn_sends_the_notice(monkeypatch, tmp_path,
                                                                                    turns_on):
    async def run(pool):
        async with _manager(pool, tmp_path, monkeypatch, []) as chat:
            await _task(pool, tmp_path, origin_thread=THREAD)
            for reason, turn_off in (("manager_off", lambda: setattr(chat.manager, "_turned_off", True)),
                                     ("off", lambda: chat_settings.set_completion_turns(
                                         chat_settings.CompletionTurns.OFF))):
                await pool.execute("DELETE FROM manager_events")
                chat_settings.set_completion_turns(chat_settings.CompletionTurns.DM_ONLY)
                chat.manager._turned_off = False
                await chat.manager._handle(_goal_status())
                (due,) = await _due(chat.bus)
                turn_off()
                await chat.manager._handle(due)
                (notice,) = _drain(chat.bus, "channel-web")
                assert notice.payload["text"] == web_end_text("completed", "calc", "## Done\nadd and sub pass")
                (row,) = await _events(pool)
                assert (row["state"], row["reason"], row["sent_at"] is not None) == ("fallback", reason, True)
            assert chat.model.requests == []

    run_db(run, monkeypatch, timeout=60)


@pytest.mark.parametrize("case,reason", [("group", "group_chat"), ("handed_over", "manager_off"),
                                         ("other_manager", "manager_off"), ("turned_off", "manager_off")])
def test_ends_a_turn_cannot_tell_are_skipped_and_their_notice_sent_at_once(monkeypatch, tmp_path, turns_on, case,
                                                                          reason):
    async def run(pool):
        async with _manager(pool, tmp_path, monkeypatch, []) as chat:
            notice = _dm_notice()
            metadata: dict[str, Any] = {}
            owner = "manager"
            if case == "group":
                notice = Notice(NotifyChannel.LARK, NotifyTarget(TargetKind.CHAT, "oc_team", mention=OPEN_ID,
                                                                 group=True), "✅ 任务完成 · calc", channel_id="channel-lark")
            elif case == "handed_over":
                metadata = {"handed_over_from": "manager-review"}
            elif case == "other_manager":
                owner = "manager-review"
                await pool.execute("INSERT INTO managers (id, name, system_prompt) VALUES ($1, 'Review', 'p')",
                                   owner)
            else:
                chat.manager._turned_off = True
            await _task(pool, tmp_path, manager_id=owner)
            await chat.manager._handle(_goal_status(thread=None, notice=notice, **metadata))
            (sent,) = _drain(chat.bus, "channel-lark")
            assert sent.message_type is MessageType.CHAT_NOTICE and sent.payload == notice.payload()
            assert _drain(chat.bus, "manager") == []
            (row,) = await _events(pool)
            assert (row["state"], row["reason"], row["sent_at"] is not None) == ("skipped", reason, True)

    run_db(run, monkeypatch, timeout=60)


def test_a_replayed_end_that_waits_for_its_turn_records_once_and_sends_nothing(monkeypatch, tmp_path, turns_on):
    async def run(pool):
        async with _manager(pool, tmp_path, monkeypatch, []) as chat:
            await _task(pool, tmp_path, origin_thread=THREAD)
            envelope = _goal_status()
            await chat.manager._handle(envelope)
            await chat.manager._handle(envelope)  # the bus replays it
            assert len(await _due(chat.bus)) == 1
            assert _drain(chat.bus, "channel-web") == []
            assert [row["state"] for row in await _events(pool)] == ["pending"]

    run_db(run, monkeypatch, timeout=60)


def test_a_retired_managers_waiting_events_send_their_notices(monkeypatch, tmp_path, turns_on):
    from looper.agents.managers import ManagerFleet

    async def run(pool):
        async with _manager(pool, tmp_path, monkeypatch, []) as chat:
            await _task(pool, tmp_path, origin_thread=THREAD)
            await chat.manager._handle(_goal_status())
            (due,) = await _due(chat.bus)
            fleet = ManagerFleet.__new__(ManagerFleet)
            fleet._supervisor = SimpleNamespace(bus=chat.bus)
            await fleet._deal_with("manager", [due])  # the Manager was turned off with the wake-up queued
            (notice,) = _drain(chat.bus, "channel-web")
            assert notice.payload["text"] == web_end_text("completed", "calc", "## Done\nadd and sub pass")
            (row,) = await _events(pool)
            assert (row["state"], row["reason"]) == ("fallback", "manager_off")

    run_db(run, monkeypatch, timeout=60)


def test_the_completion_message_names_each_ended_task_and_the_contract() -> None:
    rows = [{"task_id": "t1", "payload": {"name": "calc", "status": "completed", "fail_kind": None,
                                          "result": {"lead": "Added sub.\nTests pass.", "delivery_summary": "",
                                                     "branch": "Branch looper/t1 of the repo"}}},
            {"task_id": "t2", "payload": {"name": "docs", "status": "failed", "fail_kind": "task_budget",
                                          "result": {"lead": "Stopped at the cap.", "delivery_summary": ""}}}]
    message = completion_events.completion_message(
        rows, channel_line="Channel: a Lark direct message",
        more={"t1": {"project": "calc", "duration": "42 min", "cost_usd": 1.84}, "t2": {}})
    assert message.splitlines()[:10] == [
        "[COMPLETION] Looper reports tasks that ended. This is not a message from the user, and nobody asked you "
        "anything.",
        "Channel: a Lark direct message",
        "Ended tasks (2):",
        '1. t1 "calc" (project calc): completed in 42 min, $1.84; asked for in this chat.',
        "   Result (the start of its summary): Added sub.",
        "   Tests pass.",
        "   Branch: Branch looper/t1 of the repo",
        '2. t2 "docs": failed at its spend cap; asked for in this chat.',
        "   Result (the start of its summary): Stopped at the cap.",
        ""]
    assert message.endswith(completion_events.CONTRACT)
    links = completion_events.link_block(
        [{**row, "channel_id": "channel-telegram"} for row in rows], base="http://127.0.0.1:8767")
    assert links.splitlines() == ["「calc」http://127.0.0.1:8767/#tasks/t1", "Branch looper/t1 of the repo",
                                  "「docs」http://127.0.0.1:8767/#tasks/t2"]


def test_the_switch_changes_no_prompt_tool_or_command_of_a_users_turn(monkeypatch, tmp_path):
    from tests.test_manager_tools_db import _converse

    async def run(pool):
        seen = []
        for turns in chat_settings.CompletionTurns:
            chat_settings.set_completion_turns(turns)
            root = tmp_path / turns.value
            root.mkdir()
            await pool.execute("DELETE FROM agent_sessions")  # each run starts the Manager's session anew
            chat = await _converse(root, [[("looper_tasks", {"action": "list"})]], ["我有哪些任务？"])
            prompt = next(Path(arg) for arg in chat.argv if arg.endswith(".md") and Path(arg).exists())
            def plain(text: str) -> str:  # the run's own directory, and its endpoint's MCP entry (port, token)
                text = text.replace(str(root.resolve()), "<root>").replace(str(root), "<root>")
                return re.sub(r"\.local/mcp/[0-9a-f]{64}\.json", ".local/mcp/<entry>.json", text)

            seen.append((plain(json.dumps(chat.argv)), chat.tools, chat.turns,
                         plain(prompt.read_text(encoding="utf-8"))))
        assert seen[0] == seen[1]

    run_db(run, monkeypatch, timeout=90)


@pytest.mark.parametrize("cut", ["turn", "reply"])
def test_a_turn_a_restart_cut_off_settles_before_a_replayed_wake_up(monkeypatch, tmp_path, turns_on, cut):
    from looper.agents.tiers.manager import ManagerAgent, ManagerConfig
    from looper.db import manager_events as store
    from tests.db_support import until

    async def run(pool):
        async with _manager(pool, tmp_path, monkeypatch, []) as chat:
            await _task(pool, tmp_path, origin_thread=THREAD)
            await chat.manager._handle(_goal_status())
            (due,) = await _due(chat.bus)
            # The turn took the event (and, for "reply", was handing its reply over), then the server stopped.
            batch = uuid.uuid4()
            (taken,) = await store.claim("manager", "channel-web", THREAD, batch)
            if cut == "reply":
                await store.claim_reply(batch)
            restarted = ManagerAgent(config=ManagerConfig(work_dir=tmp_path), runtime_root=tmp_path)
            chat.bus.get_queue("manager").put_nowait(due)  # the journal replays the wake-up
            await restarted.start(chat.bus)
            try:
                await until(lambda: chat.bus.get_queue("manager").qsize() == 0 and restarted._handling is None,
                            "the replayed wake-up")
            finally:
                await restarted.stop()
            (row,) = await _events(pool)
            if cut == "reply":  # the reply may have gone: nothing more is sent, ever
                assert (row["state"], row["delivery"], row["sent_at"]) == ("delivered", "uncertain", None)
                await _service(chat.bus).run_once()
                assert _drain(chat.bus, "channel-web") == []
                return
            assert (row["id"], row["state"], row["reason"], row["delivery"]) == (
                taken["id"], "fallback", "restart", "pending")
            await _service(chat.bus).run_once()  # the completion service delivers it
            (notice,) = _drain(chat.bus, "channel-web")
            assert notice.payload["text"] == web_end_text("completed", "calc", "## Done\nadd and sub pass")
            (row,) = await _events(pool)
            assert (row["delivery"], row["sent_at"] is not None) == ("sent", True)

    run_db(run, monkeypatch, timeout=60)


def test_a_wake_up_whose_events_cannot_be_read_is_queued_again(monkeypatch, tmp_path, turns_on):
    from looper.db import manager_events as store

    real_claim, failures = store.claim, []

    async def claim(*args: Any, **kwargs: Any) -> Any:
        if not failures:
            failures.append(1)
            raise ConnectionError("the database went away")
        return await real_claim(*args, **kwargs)

    monkeypatch.setattr(store, "claim", claim)
    monkeypatch.setattr(completion_events, "RETRY_SECONDS", 0.01, raising=False)

    async def run(pool):
        async with _manager(pool, tmp_path, monkeypatch, [_text(REPLY)]) as chat:
            await _task(pool, tmp_path, origin_thread=THREAD)
            await chat.manager._handle(_goal_status())
            (due,) = await _due(chat.bus)
            await chat.manager._handle(due)
            (again,) = _drain(chat.bus, "manager")  # queued before the first is acknowledged
            assert again.message_type is MessageType.COMPLETION_DUE and again.payload == due.payload
            assert _drain(chat.bus, "channel-web") == [] and (await _events(pool))[0]["state"] == "pending"
            await chat.manager._handle(again)
            (reply,) = _drain(chat.bus, "channel-web")
            assert reply.payload["text"].startswith(REPLY)
            assert (await _events(pool))[0]["state"] == "delivered"

    run_db(run, monkeypatch, timeout=60)


class _Hung:
    """A resident session stand-in run per turn: it cannot be interrupted, and its completion turn ignores its stop
    and waits for ``release`` (a cancellation ends it, unless ``swallow`` makes it ignore that too)."""

    label = "manager"
    pid = None
    shares_bridge = False
    can_interrupt = False

    def __init__(self, *, swallow: bool = False, hold: asyncio.Event | None = None) -> None:
        self.started, self.starts, self.stops, self.swallow = False, 0, 0, swallow
        self.command: list[str] = []
        self.totals = Usage()
        self.texts: list[str] = []
        self.release = asyncio.Event()
        self.hold = hold  # a user's turn waits for it (the test's moment to end a late turn meanwhile)
        self.holding = asyncio.Event()

    def stale(self, signature: Any) -> bool:
        return False

    async def start(self, command: list[str], **kwargs: Any) -> None:
        self.started, self.command = True, command
        self.starts += 1

    async def run_turn(self, text: str, on_event: Any, *, tag: Any = None, stop: Any = None,
                       cancel: Any = None) -> Any:
        self.texts.append(text)
        await on_event({"type": "system", "subtype": "init", "session_id": "sess-1"})
        if text.startswith("[COMPLETION]"):
            while not self.release.is_set():
                try:
                    await self.release.wait()
                except asyncio.CancelledError:
                    if not self.swallow:
                        raise
            # A late exit: its transport's callback, a new session's id and spend: none of them is the Manager's now.
            await on_event({"type": "system", "subtype": "transport"})
            await on_event({"type": "system", "subtype": "init", "session_id": "sess-late"})
            self.totals = Usage(cost_usd=9.0)
        elif self.hold is not None:
            self.holding.set()
            await self.hold.wait()
        return ResidentTurn({"type": "result", "subtype": "success", "is_error": False, "result": "好的",
                             "session_id": "sess-1"})

    async def stop(self, *, kill: bool = False) -> None:
        self.started = False
        self.stops += 1


@pytest.mark.parametrize("swallow", [False, True], ids=["cancelled", "ignores_cancel"])
def test_a_turn_past_its_deadline_on_a_runtime_without_interrupt_does_not_hold_the_next_message(
        monkeypatch, tmp_path, turns_on, swallow):
    from looper.agents.runtimes.claude_code import ClaudeCodeRuntime
    from looper.agents.tiers.manager import ManagerAgent, ManagerConfig

    monkeypatch.setattr(completion_events, "TURN_SECONDS", 0.5)
    monkeypatch.setattr(completion_events, "END_SECONDS", 0.3, raising=False)
    hung, fresh = _Hung(swallow=swallow), _Hung()
    monkeypatch.setattr(ClaudeCodeRuntime, "resident_session", lambda self, label: fresh)
    claude = tmp_path / "claude"
    claude.write_text("#!/bin/sh\nexit 0\n")
    claude.chmod(0o755)

    async def run(pool):
        from looper.agents.bus import MessageBus

        await _task(pool, tmp_path, origin_thread=THREAD)
        bus = MessageBus(runtime_root=tmp_path)
        manager = ManagerAgent(config=ManagerConfig(claude_bin=str(claude), work_dir=tmp_path),
                               runtime_root=tmp_path)
        manager._bus, manager._claude = bus, hung
        await manager._handle(_goal_status())
        (due,) = await _due(bus)
        await asyncio.wait_for(manager._handle(due), 10)  # the message loop goes on
        (notice,) = _drain(bus, "channel-web")
        assert notice.payload["text"] == web_end_text("completed", "calc", "## Done\nadd and sub pass")
        (row,) = await _events(pool)
        assert (row["state"], row["reason"]) == ("fallback", "turn_timeout")
        assert not manager.busy and hung.stops >= 1  # the late turn's runtime is stopped
        await asyncio.wait_for(manager._handle(MessageEnvelope.create(
            source=channel_address("web"), target=MANAGER_ADDRESS, message_type=MessageType.USER_MESSAGE,
            payload={"text": "还在吗？", "channel_context": {"session_id": THREAD}}, correlation_id="c2",
            metadata={"origin_channel_id": "channel-web"})), 10)
        (reply,) = [e for e in _drain(bus, "channel-web") if e.message_type is MessageType.AGENT_REPLY]
        assert reply.payload["text"] == "好的"
        runtime = fresh if swallow else hung  # a fresh runtime: a new session, or the old one started again
        assert runtime.texts[-1].endswith("还在吗？") and runtime.starts == (1 if swallow else 2)
        hung.release.set()
        # The released turn still has to finish its PostgreSQL record.
        await until(lambda: not manager._late_turns, "the released completion turn to finish", timeout=10)
        await bus.shutdown()

    run_db(run, monkeypatch, timeout=60)


def test_a_turn_let_go_of_that_exits_late_touches_nothing_of_the_turn_after_it(monkeypatch, tmp_path, turns_on):
    from looper.agents.runtimes.claude_code import ClaudeCodeRuntime
    from looper.agents.tiers.manager import ManagerAgent, ManagerConfig

    monkeypatch.setattr(completion_events, "TURN_SECONDS", 0.5)
    monkeypatch.setattr(completion_events, "END_SECONDS", 0.3)
    hold = asyncio.Event()
    late, fresh = _Hung(swallow=True), _Hung(hold=hold)
    monkeypatch.setattr(ClaudeCodeRuntime, "resident_session", lambda self, label: fresh)
    claude = tmp_path / "claude"
    claude.write_text("#!/bin/sh\nexit 0\n")
    claude.chmod(0o755)

    async def run(pool):
        from looper.agents.bus import MessageBus

        await _task(pool, tmp_path, origin_thread=THREAD)
        bus = MessageBus(runtime_root=tmp_path)
        manager = ManagerAgent(config=ManagerConfig(claude_bin=str(claude), work_dir=tmp_path),
                               runtime_root=tmp_path)
        manager._bus, manager._claude = bus, late
        bindings: list[Any] = []
        publish = manager._publish_binding
        monkeypatch.setattr(manager, "_publish_binding", lambda token: (bindings.append(token), publish(token)))
        await manager._handle(_goal_status())
        (due,) = await _due(bus)
        await asyncio.wait_for(manager._handle(due), 10)  # let go of: it swallowed the cancellation
        (late_run,) = [task for task in manager._late_turns if not task.get_coro().__name__ == "stop"] or [None]
        assert manager._claude is fresh and late_run is not None
        session = manager._sessions["main"].session_id
        asking = asyncio.create_task(manager._handle(MessageEnvelope.create(
            source=channel_address("web"), target=MANAGER_ADDRESS, message_type=MessageType.USER_MESSAGE,
            payload={"text": "还在吗？", "channel_context": {"session_id": THREAD}}, correlation_id="c2",
            metadata={"origin_channel_id": "channel-web"})))
        await asyncio.wait_for(fresh.holding.wait(), 10)
        token, bound = manager._running_token, len(bindings)
        assert token is not None and token.live
        late.release.set()  # the late turn exits now, while the next one runs
        done, _ = await asyncio.wait({late_run}, timeout=10)
        assert done and isinstance(late_run.exception(), TurnStopped)  # it ends as stopped, on its own record
        assert manager._running_token is token and token.live  # its token, its binding and its runtime are its own
        assert bindings[bound:] == [] and fresh.stops == 0 and fresh.started
        assert manager._sessions["main"].session_id == session and manager._session_totals.cost_usd != 9.0
        hold.set()
        await asyncio.wait_for(asking, 10)
        (reply,) = [e for e in _drain(bus, "channel-web") if e.message_type is MessageType.AGENT_REPLY
                    and "chat_session_id" not in e.payload]
        assert reply.payload["text"] == "好的"
        await bus.shutdown()

    run_db(run, monkeypatch, timeout=60)


@pytest.mark.parametrize("known", [True, False], ids=["another_task_recorded_it", "unknown"])
def test_a_lark_direct_chat_the_task_did_not_record_is_resolved_or_told_by_its_notice(monkeypatch, tmp_path,
                                                                                      turns_on, known):
    gate = asyncio.Event()
    script = ResponseScript(events=text_events(REPLY, usage=USAGE), gate=gate)

    async def run(pool):
        async with _manager(pool, tmp_path, monkeypatch, [script]) as chat:
            await _operator_is_the_lark_person(pool)
            await _task(pool, tmp_path)  # recorded no reply route
            if known:  # the same person's earlier task with this bot recorded their direct chat
                project = await pool.fetchval("SELECT project_id FROM tasks WHERE id = 't1'")
                await tasks.create_task(task_id="t0", requirement="r", project_id=project, work_dir=str(tmp_path),
                                        status="completed", pool=pool, origin_channel_id="channel-lark",
                                        chat_id=OPEN_ID, chat_kind="user", origin_reply_route={"chat_id": DM_CHAT})
            await chat.manager._handle(_goal_status(thread=None, notice=_dm_notice(),
                                                    origin_channel_id="channel-lark"))
            (row,) = await _events(pool)
            if not known:
                assert (row["state"], row["reason"], row["sent_at"] is not None) == ("skipped", "no_chat", True)
                (sent,) = _drain(chat.bus, "channel-lark")
                assert sent.payload == _dm_notice().payload()
                return
            assert row["state"] == "pending" and json.loads(row["payload"])["chat_id"] == DM_CHAT
            (due,) = await _due(chat.bus)
            telling = asyncio.create_task(chat.manager._handle(due))
            await asyncio.wait_for(script.gate_reached.wait(), 20)
            (token,) = chat.manager.live_for_chat("channel-lark", DM_CHAT)  # the turn /stop in that chat finds
            assert token.chat == DM_CHAT
            gate.set()
            await asyncio.wait_for(telling, 20)
            (sent,) = _drain(chat.bus, "channel-lark")
            assert sent.metadata["chat_record"]["session_id"] == f"lark:{DM_CHAT}"

    run_db(run, monkeypatch, timeout=60)
