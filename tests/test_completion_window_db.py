"""The completion service (manager completion ledger RFC, C3): a chat's task ends gather for a minute into one
completion turn, a turn's chat is not woken again while it runs, a user's message queued first is answered first,
an event that waits 15 minutes falls back, and a schedule run's message is told (or falls back) through the
schedule's outbox, which names the event.

Real PostgreSQL; runs only when LOOPER_TEST_DATABASE_URL points at a disposable database.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from looper.agents import chat_settings, completion_events
from looper.agents.completion_service import CompletionService
from looper.agents.message import MessageType
from looper.agents.task_notice import web_end_text
from looper.db import manager_events as store, projects, schedules as db_schedules, tasks
from tests.api_manager_support import USAGE, _manager, _message
from tests.db_support import DSN, run_db, until
from tests.fake_model_api import ResponseScript, text_events
from tests.schedule_support import TELEGRAM_CHAT, ScheduleEnv, allow_telegram, run_scheduled, with_sender
from tests.test_completion_turn_db import THREAD, _drain, _events, _goal_status, _input

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")

REPLY = "三个任务都完成了。"


@pytest.fixture(autouse=True)
def turns_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(chat_settings, "_completion_turns", chat_settings.CompletionTurns.DM_ONLY)


def _later(seconds: float) -> datetime:
    return datetime.now(timezone.utc) + timedelta(seconds=seconds)


def _service(bus: Any, **kwargs: Any) -> CompletionService:
    return CompletionService(bus=bus, manager_running=kwargs.pop("running", lambda manager_id: True),
                             running_managers=lambda: ["manager"], **kwargs)


async def _tasks(pool: Any, root: Path, *ids: str) -> None:
    project = await projects.create_project(name="calc", work_dir=str(root), pool=pool)
    for task_id in ids:
        await tasks.create_task(task_id=task_id, requirement="r", project_id=project["id"], work_dir=str(root),
                                status="completed", title=f"calc {task_id}", pool=pool, origin_thread=THREAD)


def _end(task_id: str):
    envelope = _goal_status()
    envelope.payload["task_id"] = task_id
    envelope.metadata["task_title"] = f"calc {task_id}"
    return envelope


def _wakes(bus: Any) -> list[Any]:
    return [e for e in _drain(bus, "manager") if e.message_type is MessageType.COMPLETION_DUE]


def test_tasks_ending_within_the_window_are_told_in_one_turn(monkeypatch, tmp_path):
    async def run(pool):
        async with _manager(pool, tmp_path, monkeypatch, [ResponseScript(events=text_events(REPLY, usage=USAGE))]) \
                as chat:
            await _tasks(pool, tmp_path, "t1", "t2", "t3")
            for task_id in ("t1", "t2", "t3"):
                await chat.manager._handle(_end(task_id))
            service = _service(chat.bus)
            assert await service.run_once(now=_later(30)) == 0 and _wakes(chat.bus) == []  # the window is open
            assert await service.run_once(now=_later(61)) == 1
            (due,) = _wakes(chat.bus)
            assert await service.run_once(now=_later(62)) == 0  # not queued again within 30 seconds
            await chat.manager._handle(due)
            (request,) = chat.model.requests
            assert "Ended tasks (3):" in _input(request)
            (reply,) = _drain(chat.bus, "channel-web")
            assert reply.payload["text"].startswith(REPLY)
            assert [line.split("]")[0] for line in reply.payload["text"].splitlines()[2:]] == [
                "「calc t1」[/#tasks/t1", "「calc t2」[/#tasks/t2", "「calc t3」[/#tasks/t3"]
            rows = await _events(pool)
            assert {row["state"] for row in rows} == {"delivered"} and len({row["batch"] for row in rows}) == 1

    run_db(run, monkeypatch, timeout=60)


def test_an_end_during_a_turn_waits_for_the_next_one(monkeypatch, tmp_path):
    gate = asyncio.Event()
    scripts = [ResponseScript(events=text_events("t1 完成了。", usage=USAGE), gate=gate),
               ResponseScript(events=text_events("t2 也完成了。", usage=USAGE))]

    async def run(pool):
        async with _manager(pool, tmp_path, monkeypatch, scripts) as chat:
            await _tasks(pool, tmp_path, "t1", "t2")
            await chat.manager._handle(_end("t1"))
            service = _service(chat.bus)
            await service.run_once(now=_later(61))
            (due,) = _wakes(chat.bus)
            telling = asyncio.create_task(chat.manager._handle(due))
            await asyncio.wait_for(scripts[0].gate_reached.wait(), 20)
            await chat.manager._handle_goal_status(_end("t2"))  # ends while the first turn runs
            assert await _service(chat.bus).run_once(now=_later(200)) == 0  # its chat has a turn running
            gate.set()
            await asyncio.wait_for(telling, 20)
            (first,) = _drain(chat.bus, "channel-web")
            assert "/#tasks/t1" in first.payload["text"] and "/#tasks/t2" not in first.payload["text"]
            await _service(chat.bus).run_once(now=_later(61))
            (again,) = _wakes(chat.bus)
            await chat.manager._handle(again)
            (second,) = _drain(chat.bus, "channel-web")
            assert second.payload["text"].startswith("t2 也完成了。") and "/#tasks/t2" in second.payload["text"]
            rows = await _events(pool)
            assert [row["state"] for row in rows] == ["delivered", "delivered"]
            assert rows[0]["batch"] != rows[1]["batch"]

    run_db(run, monkeypatch, timeout=90)


def test_a_users_message_queued_first_is_answered_first(monkeypatch, tmp_path):
    scripts = [ResponseScript(events=text_events("先回答你。", usage=USAGE)),
               ResponseScript(events=text_events(REPLY, usage=USAGE))]

    async def run(pool):
        async with _manager(pool, tmp_path, monkeypatch, scripts) as chat:
            await _tasks(pool, tmp_path, "t1")
            await chat.manager._handle(_end("t1"))
            chat.bus.get_queue("manager").put_nowait(_message("现在怎么样？"))
            await _service(chat.bus).run_once(now=_later(61))  # its wake-up queues behind the message
            await chat.manager.start(chat.bus)
            replies: list[Any] = []

            def answered() -> bool:
                replies.extend(e for e in _drain(chat.bus, "channel-web") if e.message_type is MessageType.AGENT_REPLY)
                return len(replies) >= 2

            await until(answered, "both replies", timeout=30)
            assert [reply.payload["text"].split("\n")[0] for reply in replies] == ["先回答你。", REPLY]
            assert "[COMPLETION]" not in _input(chat.model.requests[0])
            assert "[COMPLETION]" in _input(chat.model.requests[1])

    run_db(run, monkeypatch, timeout=60)


def test_an_event_that_waits_fifteen_minutes_gets_its_notice(monkeypatch, tmp_path):
    async def run(pool):
        async with _manager(pool, tmp_path, monkeypatch, []) as chat:
            await _tasks(pool, tmp_path, "t1")
            await chat.manager._handle(_end("t1"))
            service = _service(chat.bus)
            await service.run_once(now=_later(61))
            assert len(_wakes(chat.bus)) == 1  # the Manager is held by a long turn: it never takes it
            assert await service.run_once(now=_later(store.QUEUE_SECONDS - 5)) == 0
            assert _drain(chat.bus, "channel-web") == []
            await service.run_once(now=_later(store.QUEUE_SECONDS + 1))
            (notice,) = _drain(chat.bus, "channel-web")
            assert notice.payload["text"] == web_end_text("completed", "calc t1", "## Done\nadd and sub pass")
            (row,) = await _events(pool)
            assert (row["state"], row["reason"], row["sent_at"] is not None) == ("fallback", "queue_deadline", True)
            assert _wakes(chat.bus) == []  # nothing left to tell

    run_db(run, monkeypatch, timeout=60)


def test_a_manager_that_does_not_run_gets_its_chats_notices(monkeypatch, tmp_path):
    async def run(pool):
        async with _manager(pool, tmp_path, monkeypatch, []) as chat:
            await _tasks(pool, tmp_path, "t1")
            await chat.manager._handle(_end("t1"))
            await _service(chat.bus, running=lambda manager_id: False).run_once(now=_later(61))
            assert _wakes(chat.bus) == []
            (notice,) = _drain(chat.bus, "channel-web")
            assert notice.payload["text"] == web_end_text("completed", "calc t1", "## Done\nadd and sub pass")
            assert [(r["state"], r["reason"]) for r in await _events(pool)] == [("fallback", "manager_off")]

    run_db(run, monkeypatch, timeout=60)


def test_after_a_restart_pending_events_are_told_and_a_stopped_managers_running_ones_fall_back(monkeypatch,
                                                                                            tmp_path):
    async def run(pool):
        async with _manager(pool, tmp_path, monkeypatch, []) as chat:
            await _tasks(pool, tmp_path, "t1", "t2")
            await chat.manager._handle(_end("t1"))
            await pool.execute("INSERT INTO managers (id, name, system_prompt) VALUES ('manager-off', 'Off', 'p')")
            (taken,) = await store.claim("manager", "channel-web", THREAD, uuid.uuid4())
            await pool.execute("UPDATE manager_events SET manager_id = 'manager-off' WHERE id = $1", taken["id"])
            await chat.manager._handle(_end("t2"))  # pending when the server stopped
            service = _service(chat.bus)  # the new server's: manager-off does not run
            await service.start()
            try:
                await until(lambda: chat.bus.get_queue("channel-web").qsize() == 1, "the cut-off turn's notice")
                (notice,) = _drain(chat.bus, "channel-web")
                assert notice.payload["text"] == web_end_text("completed", "calc t1", "## Done\nadd and sub pass")
                await service.run_once(now=_later(61))
                (due,) = _wakes(chat.bus)
                assert due.payload == {"manager_id": "manager", "channel_id": "channel-web", "chat": THREAD}
            finally:
                await service.stop()
            rows = {row["task_id"]: row for row in await _events(pool)}
            assert (rows["t1"]["state"], rows["t1"]["reason"]) == ("fallback", "restart")
            assert rows["t2"]["state"] == "pending"

    run_db(run, monkeypatch, timeout=60)


def test_the_service_looks_at_once_when_an_end_is_recorded(monkeypatch, tmp_path):
    async def run(pool):
        async with _manager(pool, tmp_path, monkeypatch, []) as chat:
            await _tasks(pool, tmp_path, "t1")
            monkeypatch.setattr(store, "WINDOW_SECONDS", 0)
            service = _service(chat.bus, interval=3600)
            await service.start()
            try:
                await asyncio.sleep(0.1)  # its first pass, with nothing to do
                await chat.manager._handle(_end("t1"))
                await until(lambda: chat.bus.get_queue("manager").qsize() == 1, "the wake-up", timeout=5)
            finally:
                await service.stop()
            assert len(_wakes(chat.bus)) == 1

    run_db(run, monkeypatch, timeout=60)


def test_the_completion_turns_fail_once_five_batches_in_a_row_fell_back(monkeypatch, tmp_path):
    async def run(pool):
        await _tasks(pool, tmp_path, *(f"t{n}" for n in range(6)))
        for n in range(6):
            await store.record(manager_id="manager", task_id=f"t{n}", origin="chat", target_kind="web",
                               channel_id="channel-web", chat=f"s{n}", dedupe_key=f"k{n}", payload={}, fallback=[],
                               state="pending", reason=None)
            await store.claim("manager", "channel-web", f"s{n}", uuid.uuid4())
        batches = [row["batch"] for row in await pool.fetch("SELECT batch FROM manager_events ORDER BY id")]
        await store.deliver(batches[0], record_id=None)
        for batch in batches[1:5]:
            await store.fall_back(reason="turn_timeout", batch=batch)
        assert not await store.failing()  # four in a row
        await store.fall_back(reason="empty_reply", batch=batches[5])
        assert await store.failing()
        assert (await store.summary())["failing"] is True

    run_db(run, monkeypatch, timeout=60)


# --- Schedules ---

async def _failed_run(env: ScheduleEnv, *, notify: str = "failures", status: str = "failed"):
    sender = with_sender(env)
    completion_events.set_schedule_delivery(env.scheduler.deliver_run_message)
    await allow_telegram(env)
    schedule = await env.schedule(notify=notify, notify_target={"channel": "telegram", "chat": TELEGRAM_CHAT})
    run_id = await env.scheduler.run_now(schedule["schedule_id"])
    run = await db_schedules.get_run(run_id)
    await env.end_task(run["task_id"], status, summary="the report could not be written")
    await env.scheduler.idle()
    return sender, schedule, run


async def _outbox(env: ScheduleEnv) -> list[dict[str, Any]]:
    return [dict(row) for row in await env.pool.fetch(
        "SELECT kind, text, status, manager_event_id FROM schedule_notifications WHERE kind = 'run' ORDER BY created_at")]


@pytest.fixture
def schedule_delivery():
    yield
    completion_events.set_schedule_delivery(None)


def test_a_successful_run_under_notify_failures_records_no_event(monkeypatch, tmp_path, schedule_delivery):
    async def test(env: ScheduleEnv):
        sender, *_ = await _failed_run(env, status="completed")
        assert sender.sent == [] and await _events(env.pool) == []

    run_scheduled(test, monkeypatch, tmp_path)


def test_a_failed_runs_message_waits_for_the_turn_and_its_reply_is_the_outbox_row(monkeypatch, tmp_path,
                                                                                  schedule_delivery):
    async def test(env: ScheduleEnv):
        sender, schedule, run = await _failed_run(env)
        assert sender.sent == [] and await _outbox(env) == []  # no code notice: the completion turn tells it
        (row,) = await _events(env.pool)
        assert (row["origin"], row["target_kind"], row["state"]) == ("schedule", "dm", "pending")
        assert json.loads(row["payload"])["result"]["lead"] == "the report could not be written"
        rows = await store.claim("manager", "channel-telegram", TELEGRAM_CHAT, uuid.uuid4())
        message = completion_events.completion_message(rows, channel_line="Channel: a Telegram direct message",
                                                       more={})
        assert f'1. {run["task_id"]}: failed; a run of the schedule "{schedule["name"]}", whose messages come to ' \
               "this chat." in message
        await completion_events.send_reply(None, None, rows, "日报没写出来：数据源不可用。要我再跑一次吗？")
        assert [text for _, _, text in sender.sent] == ["日报没写出来：数据源不可用。要我再跑一次吗？"]
        assert await _outbox(env) == [{"kind": "run", "text": "日报没写出来：数据源不可用。要我再跑一次吗？",
                                       "status": "sent", "manager_event_id": row["id"]}]

    run_scheduled(test, monkeypatch, tmp_path)


def test_a_failed_runs_fallback_is_its_message_with_the_event_in_the_outbox(monkeypatch, tmp_path,
                                                                            schedule_delivery):
    async def test(env: ScheduleEnv):
        sender, schedule, run = await _failed_run(env)
        (row,) = await _events(env.pool)
        expected = json.loads(row["fallback"])[0]["text"]
        await _service(env.bus).run_once(now=_later(store.QUEUE_SECONDS + 1))
        assert [text for _, _, text in sender.sent] == [expected]
        assert await _outbox(env) == [{"kind": "run", "text": expected, "status": "sent",
                                       "manager_event_id": row["id"]}]
        (row,) = await _events(env.pool)
        assert (row["state"], row["reason"], row["sent_at"] is not None) == ("fallback", "queue_deadline", True)

    run_scheduled(test, monkeypatch, tmp_path)


def test_with_the_switch_off_a_runs_message_is_sent_with_no_event_in_the_outbox(monkeypatch, tmp_path,
                                                                                schedule_delivery):
    monkeypatch.setattr(chat_settings, "_completion_turns", chat_settings.CompletionTurns.OFF)

    async def test(env: ScheduleEnv):
        sender, *_ = await _failed_run(env)
        assert len(sender.sent) == 1
        (note,) = await _outbox(env)
        assert note["manager_event_id"] is None and note["status"] == "sent"
        assert [(r["state"], r["reason"]) for r in await _events(env.pool)] == [("skipped", "off")]

    run_scheduled(test, monkeypatch, tmp_path)


def test_health_shows_the_services_last_counts_from_memory(monkeypatch, tmp_path):
    from looper.agents import completion_service

    async def run(pool):
        async with _manager(pool, tmp_path, monkeypatch, []) as chat:
            await _tasks(pool, tmp_path, "t1")
            await chat.manager._handle(_end("t1"))
            assert completion_service.health() is None  # no service runs
            service = _service(chat.bus, interval=3600)
            await service.start()
            try:
                await until(lambda: completion_service.health() is not None, "the first pass")
                found = completion_service.health()
                assert (found["pending"], found["by_state"], found["failing"]) == (1, {"pending": 1}, False)
                assert found["oldest_pending_seconds"] is not None and found["checked_at"]
            finally:
                await service.stop()
            assert completion_service.health() is None

    run_db(run, monkeypatch, timeout=60)
