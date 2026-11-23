"""Manager lanes through the bus, real records and a loopback API runtime."""
from __future__ import annotations

import asyncio
import json
import time

import pytest

from looper.agents import chat_settings, completion_events, lanes_settings, session_routing, turn_metrics
from looper.agents.bus import MessageBus
from looper.agents.message import MANAGER_ADDRESS, TASK_ROUTER_ADDRESS, MessageEnvelope, MessageType, channel_address
from looper.agents.runtimes.api.session import ApiSession
from looper.agents.tiers.manager import ManagerAgent, ManagerConfig
from looper.agents.task_notice import web_end_text
from looper.db import projects, tasks
from tests.api_manager_support import USAGE, _manager
from tests.db_support import DSN, run_db, until
from tests.fake_model_api import ResponseScript, text_events

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")
THREAD = "lanes-test-chat"
SUMMARY = "## Done\nSynthetic result"
NOTICE = web_end_text("completed", "lanes-task", SUMMARY)


def _script(text, gate=None):
    return ResponseScript(events=text_events(text, usage=USAGE), gate=gate)


def _user(text="hello"):
    return MessageEnvelope.create(
        source=channel_address("web"), target=MANAGER_ADDRESS, message_type=MessageType.USER_MESSAGE,
        payload={"text": text, "channel_context": {"session_id": THREAD}},
        metadata={"origin_channel_id": "channel-web"})


async def _record_end(chat, pool, root):
    project = await projects.create_project(name="lanes-project", work_dir=str(root), pool=pool)
    await tasks.create_task(task_id="lanes-task", requirement="synthetic task", project_id=project["id"],
                            work_dir=str(root), status="completed", title="lanes-task", pool=pool,
                            origin_thread=THREAD)
    await chat.manager._handle(MessageEnvelope.create(
        source=TASK_ROUTER_ADDRESS, target=MANAGER_ADDRESS, message_type=MessageType.GOAL_STATUS,
        payload={"task_id": "lanes-task", "goal_id": "lanes-goal", "status": "completed", "summary": SUMMARY},
        metadata={"task_title": "lanes-task", "origin_thread": THREAD, "origin_channel_id": "channel-web"}))
    return completion_events.wake(MANAGER_ADDRESS, completion_events.Target("manager", "channel-web", THREAD))


def _observe_settles(bus, monkeypatch):
    settled = []
    original = bus.settle_handled

    def settle(envelope):
        settled.append(envelope.message_id)
        original(envelope)

    monkeypatch.setattr(bus, "settle_handled", settle)
    return settled


def _replies(bus):
    queue = bus.get_queue("channel-web")
    return [item for item in (queue.get_nowait() for _ in range(queue.qsize()))
            if item.message_type is MessageType.AGENT_REPLY]


@pytest.fixture(autouse=True)
def _switches(monkeypatch):
    monkeypatch.setattr(chat_settings, "_completion_turns", chat_settings.CompletionTurns.DM_ONLY)
    monkeypatch.setattr(chat_settings, "_metrics_enabled", True)
    monkeypatch.setattr(lanes_settings, "enabled", lambda: True)
    monkeypatch.setattr(session_routing, "routes", lambda _: False)


@pytest.mark.parametrize("interruptible", [True, False])
def test_user_arrival_preempts_or_waits_for_the_completion_runtime(monkeypatch, tmp_path, interruptible):
    monkeypatch.setattr(ApiSession, "can_interrupt", interruptible)

    async def run(pool):
        gate = asyncio.Event()
        completion = _script("background answer", gate)
        async with _manager(pool, tmp_path, monkeypatch, [completion, _script("user answer")]) as chat:
            due = await _record_end(chat, pool, tmp_path)
            settled = _observe_settles(chat.bus, monkeypatch)
            await chat.bus.start()
            await chat.manager.start(chat.bus)
            await chat.bus.publish(due)
            await asyncio.wait_for(completion.gate_reached.wait(), 15)
            token = chat.manager._sessions["main"].running_token
            assert token is not None and token.live
            assert chat.manager._handling is None  # completion is on main's worker, never the bus loop
            user = _user()
            arrived_at = time.monotonic()
            await chat.bus.publish(user)
            await until(lambda: chat.manager._router.queued_count(chat.manager._sessions["main"]) == 1
                        or token.cancel.requested, "user arrival reached main")
            if interruptible:
                await until(lambda: user.message_id in settled, "preempted completion then user reply")
                assert token.stop.via == "preempt"
                assert chat.model.requests[1].received_at - arrived_at < 5
                row = await pool.fetchrow("SELECT * FROM manager_events")
                assert (row["state"], row["reason"], row["delivery"]) == ("fallback", "turn_stopped", "sent")
                assert _replies(chat.bus)[0].payload["text"] == NOTICE
                lane = json.loads(await pool.fetchval("SELECT metadata FROM llm_records WHERE id = $1",
                                                     row["record_id"]))["lane"]
                assert lane == {"lane": "background", "preempted_by": user.message_id}
                request = json.dumps(chat.model.requests[1].body["input"])
                assert "your report on 1 ended task(s) was interrupted so you could answer" in request
            else:
                assert not token.cancel.requested and len(chat.model.requests) == 1
                assert due.message_id not in settled and user.message_id not in settled
                gate.set()
                await until(lambda: user.message_id in settled, "uninterruptible turn ended")
                row = await pool.fetchrow("SELECT * FROM manager_events")
                assert (row["state"], row["reason"]) == ("delivered", None)
            assert settled.index(due.message_id) < settled.index(user.message_id)
            assert not chat.manager._router.holds(chat.manager._sessions["main"])

    run_db(run, monkeypatch, timeout=60)


@pytest.mark.parametrize("routing", [False, True])
def test_user_arrival_preempts_while_completion_task_facts_are_loading(monkeypatch, tmp_path, routing):
    monkeypatch.setattr(ApiSession, "can_interrupt", True)
    monkeypatch.setattr(session_routing, "routes", lambda _: routing)

    async def run(pool):
        loading, cancelled = asyncio.Event(), asyncio.Event()

        async def slow_facts(task_ids):
            loading.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        monkeypatch.setattr(completion_events, "task_facts", slow_facts)
        async with _manager(pool, tmp_path, monkeypatch, [_script("user answer")]) as chat:
            due = await _record_end(chat, pool, tmp_path)
            settled = _observe_settles(chat.bus, monkeypatch)
            await chat.bus.start()
            await chat.manager.start(chat.bus)
            await chat.bus.publish(due)
            await asyncio.wait_for(loading.wait(), 15)
            session = chat.manager._sessions["main"]
            token = session.running_token
            user = _user()
            await chat.bus.publish(user)
            await until(lambda: chat.manager._router.queued_count(session) == 1
                        or due.message_id in settled, "arrival dispatched during task facts")
            assert token is not None and token.cancel.requested
            assert token.stop.via == "preempt" and token.preempted_by == user.message_id
            await until(lambda: user.message_id in settled, "fallback and user reply without task facts")
            assert cancelled.is_set()
            assert settled == [due.message_id, user.message_id]
            row = await pool.fetchrow("SELECT state, reason, delivery FROM manager_events")
            assert tuple(row) == ("fallback", "turn_stopped", "sent")
            assert [reply.payload["text"] for reply in _replies(chat.bus)] == [NOTICE, "user answer"]
            assert len(chat.model.requests) == 1
            assert "was interrupted so you could answer" in json.dumps(chat.model.requests[0].body["input"])

    run_db(run, monkeypatch, timeout=60)


@pytest.mark.parametrize("routing", [False, True])
def test_lanes_off_keeps_completion_input_records_and_settle_order(monkeypatch, tmp_path, routing):
    # Fixed behavior baseline: ecc2eedbf2ac4a1b44ff9078e634002c228458c1.
    monkeypatch.setattr(lanes_settings, "enabled", lambda: False)
    monkeypatch.setattr(session_routing, "routes", lambda _: routing)

    async def run(pool):
        gate = asyncio.Event()
        completion = _script("background answer", gate)
        async with _manager(pool, tmp_path, monkeypatch, [completion, _script("user answer")]) as chat:
            due = await _record_end(chat, pool, tmp_path)
            settled = _observe_settles(chat.bus, monkeypatch)
            await chat.bus.start()
            await chat.manager.start(chat.bus)
            await chat.bus.publish(due)
            await asyncio.wait_for(completion.gate_reached.wait(), 15)
            token = chat.manager._sessions["main"].running_token
            user = _user()
            await chat.bus.publish(user)
            if routing:
                await until(lambda: chat.manager._router.queued_count(chat.manager._sessions["main"]) == 1)
            else:
                assert chat.bus.pending_count("manager") == 1
            assert not token.cancel.requested and len(chat.model.requests) == 1
            gate.set()
            await until(lambda: user.message_id in settled, "off-path turns settled")
            replies = _replies(chat.bus)
            assert [reply.payload["text"] for reply in replies] == [
                "background answer\n\n[/#tasks/lanes-task](/#tasks/lanes-task)", "user answer"]
            assert settled == [due.message_id, user.message_id]
            assert "[COMPLETION] Looper reports tasks that ended." in json.dumps(chat.model.requests[0].body["input"])
            second = json.dumps(chat.model.requests[1].body["input"])
            assert "was interrupted so you could answer" not in second
            records = await pool.fetch("SELECT metadata, status FROM llm_records ORDER BY id")
            assert len(records) == 2 and all("lane" not in json.loads(record["metadata"]) for record in records)
            assert all(record["status"] != "interrupted" for record in records)

    run_db(run, monkeypatch, timeout=60)


def test_user_overtakes_queued_completion_without_a_background_wait_metric(monkeypatch, tmp_path):
    async def run(pool):
        gate = asyncio.Event()
        first = _script("first answer", gate)
        async with _manager(pool, tmp_path, monkeypatch,
                            [first, _script("second answer"), _script("completion answer")]) as chat:
            due = await _record_end(chat, pool, tmp_path)
            settled = _observe_settles(chat.bus, monkeypatch)
            await chat.bus.start()
            await chat.manager.start(chat.bus)
            one, two = _user("first user"), _user("second user")
            await chat.bus.publish(one)
            await asyncio.wait_for(first.gate_reached.wait(), 15)
            await chat.bus.publish(due)
            await until(lambda: chat.manager._router.holds_completion(due), "completion claimed and queued")
            await chat.bus.publish(two)
            await until(lambda: chat.manager._router.queued_count(chat.manager._sessions["main"]) == 2)
            gate.set()
            await until(lambda: due.message_id in settled, "both user turns then completion settled")
            assert settled == [one.message_id, two.message_id, due.message_id]
            assert "second user" in json.dumps(chat.model.requests[1].body["input"])
            assert "[COMPLETION]" in json.dumps(chat.model.requests[2].body["input"])
            await until(lambda: not turn_metrics._pending, "turn metrics writes")
            records = await pool.fetch("SELECT metadata FROM llm_records ORDER BY id")
            assert json.loads(records[1]["metadata"])["turn_metrics"]["queued_behind"] == "user"

    run_db(run, monkeypatch, timeout=60)


def test_restart_between_preemption_and_fallback_delivers_the_notice_once(monkeypatch, tmp_path):
    async def run(pool):
        gate, falling_back = asyncio.Event(), asyncio.Event()
        completion = _script("discarded background answer", gate)
        async with _manager(pool, tmp_path, monkeypatch, [completion, _script("user after restart")]) as chat:
            due = await _record_end(chat, pool, tmp_path)
            settled = _observe_settles(chat.bus, monkeypatch)
            await chat.bus.start()
            await chat.manager.start(chat.bus)

            async def cut_before_fallback(*args, **kwargs):
                falling_back.set()
                await asyncio.Event().wait()

            monkeypatch.setattr(chat.manager, "_fall_back_completion", cut_before_fallback)
            await chat.bus.publish(due)
            await asyncio.wait_for(completion.gate_reached.wait(), 15)
            await chat.bus.publish(_user("answer after restart"))
            await asyncio.wait_for(falling_back.wait(), 15)
            assert due.message_id not in settled
            assert await pool.fetchval("SELECT state FROM manager_events") == "running"
            await chat.manager.stop()
            await chat.bus.shutdown()
            restarted_bus = MessageBus(runtime_root=tmp_path)
            restarted = ManagerAgent(config=ManagerConfig(work_dir=tmp_path), runtime_root=tmp_path)
            restarted.ops = chat.ops
            try:
                await restarted_bus.start()
                await restarted.start(restarted_bus)
                await until(lambda: not restarted_bus.pending_count("manager") and not restarted.busy
                            and not restarted._router.pending(), "journal replay after restart")
                await completion_events.deliver_notices(restarted_bus)
                await completion_events.deliver_notices(restarted_bus)
                notices = [reply for reply in _replies(restarted_bus) if reply.payload["text"] == NOTICE]
                assert len(notices) == 1
                row = await pool.fetchrow("SELECT state, reason, delivery FROM manager_events")
                assert tuple(row) == ("fallback", "restart", "sent")
            finally:
                await restarted.stop()
                await restarted_bus.shutdown()

    run_db(run, monkeypatch, timeout=60)


def test_status_and_stop_still_bypass_a_background_turn(monkeypatch, tmp_path):
    from looper.agents import status_card
    monkeypatch.setattr(status_card, "_enabled", True)
    monkeypatch.setattr(chat_settings, "_stop_enabled", True)

    async def run(pool):
        completion = _script("background", asyncio.Event())
        async with _manager(pool, tmp_path, monkeypatch, [completion]) as chat:
            due = await _record_end(chat, pool, tmp_path)
            settled = _observe_settles(chat.bus, monkeypatch)
            await chat.bus.start()
            await chat.manager.start(chat.bus)
            await chat.bus.publish(due)
            await asyncio.wait_for(completion.gate_reached.wait(), 15)
            token = chat.manager._sessions["main"].running_token
            await chat.bus.publish(_user("/status"))
            await until(lambda: not chat.manager._status_tasks, "status answered")
            status = _replies(chat.bus)
            assert any("后台排队" in item.payload["text"] for item in status)
            assert not token.cancel.requested and len(chat.model.requests) == 1
            await chat.bus.publish(_user("/stop"))
            await until(lambda: due.message_id in settled, "explicit stop fallback")
            assert token.stop.via != "preempt" and token.preempted_by is None
            row = await pool.fetchrow("SELECT state, reason FROM manager_events")
            assert tuple(row) == ("fallback", "turn_stopped")
            assert len(chat.model.requests) == 1
    run_db(run, monkeypatch, timeout=60)


def test_claimed_batch_deadline_writes_notices_without_taking_the_worker(monkeypatch, tmp_path):
    from looper.db import manager_events
    monkeypatch.setattr(manager_events, "QUEUE_SECONDS", 0.2)

    async def run(pool):
        gate = asyncio.Event()
        first = _script("user answer", gate)
        async with _manager(pool, tmp_path, monkeypatch, [first, _script("second answer")]) as chat:
            due = await _record_end(chat, pool, tmp_path)
            settled = _observe_settles(chat.bus, monkeypatch)
            await chat.bus.start()
            await chat.manager.start(chat.bus)
            one, two = _user("first"), _user("second")
            await chat.bus.publish(one)
            await asyncio.wait_for(first.gate_reached.wait(), 15)
            await chat.bus.publish(two)
            await chat.bus.publish(due)
            await until(lambda: due.message_id in settled, "queued batch deadline")
            assert one.message_id not in settled and two.message_id not in settled
            row = await pool.fetchrow("SELECT state, reason, delivery FROM manager_events")
            assert tuple(row) == ("fallback", "queue_deadline", "sent")
            assert [item.payload["text"] for item in _replies(chat.bus)] == [NOTICE]
            gate.set()
            await until(lambda: two.message_id in settled, "queued user completed")
            assert len(chat.model.requests) == 2  # the expired completion never starts later
    run_db(run, monkeypatch, timeout=60)
