"""Workflow phase notices stay readable and reach their durable chat thread."""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from looper.agents.bus import MessageBus
from looper.agents.message import MessageEnvelope, MessageType, TASK_ROUTER_ADDRESS
from looper.agents.tiers.manager import ManagerAgent, ManagerConfig
from looper.agents.workflow_reporting import Phase, phase_text
from tests.test_planner_agent import REVIEW_PASS, WORK_OK, always, decide, drive, plan, step
from tests.test_review_panel_planner import BLOCK, MEMBERS, configure, held_reviews


@pytest.mark.parametrize("notices", [True, False])
def test_web_plan_created_notice_replaces_task_accepted_line(agent_db, tmp_path, notices):
    async def run():
        bus = MessageBus(runtime_root=tmp_path)
        manager = ManagerAgent(config=ManagerConfig(work_dir=tmp_path), runtime_root=tmp_path)
        manager._bus = bus
        metadata = {"origin_channel_id": "channel-web", "origin_thread": "task-origin-thread",
                    "task_title": "计算器"}
        text = phase_text(Phase.PLAN_CREATED, {"workflow": {},
            "steps": [SimpleNamespace(label="s1", instruction="Build the calculator")]})
        try:
            await manager._handle_task_accepted(MessageEnvelope.create(
                source=TASK_ROUTER_ADDRESS, target=manager.agent_address,
                message_type=MessageType.TASK_ACCEPTED,
                payload={"step_count": 1, "phase_notices": notices}, metadata=metadata))
            if notices:
                assert bus.get_queue("channel-web").empty()
                await manager._handle_goal_status(MessageEnvelope.create(
                    source=TASK_ROUTER_ADDRESS, target=manager.agent_address,
                    message_type=MessageType.GOAL_STATUS,
                    payload={"status": "executing", "phase_text": text}, metadata=metadata))
            delivered = bus.get_queue("channel-web").get_nowait()
            assert delivered.message_type is MessageType.AGENT_REPLY
            assert delivered.payload == {"chat_session_id": "task-origin-thread", "text": text if notices
                else "任务「计算器」已分解为 1 个步骤，开始执行。"}
            assert bus.get_queue("channel-web").empty()
        finally:
            await manager.stop()
            await bus.shutdown()

    asyncio.run(asyncio.wait_for(run(), 5))


def test_web_phase_notice_uses_task_origin_thread(agent_db, tmp_path):
    async def run():
        bus = MessageBus(runtime_root=tmp_path)
        manager = ManagerAgent(config=ManagerConfig(work_dir=tmp_path), runtime_root=tmp_path)
        manager._bus = bus
        try:
            envelope = MessageEnvelope.create(source=TASK_ROUTER_ADDRESS, target=manager.agent_address,
                message_type=MessageType.GOAL_STATUS,
                payload={"status": "executing", "summary": "generic progress", "phase_text": "Review ready"},
                metadata={"origin_channel_id": "channel-web", "origin_thread": "task-origin-thread",
                          "reply_route": {"message_id": "closed-creating-turn"}})
            await manager._handle_goal_status(envelope)
            delivered = bus.get_queue("channel-web").get_nowait()
            assert delivered.message_type is MessageType.AGENT_REPLY
            assert delivered.payload == {"text": "Review ready", "chat_session_id": "task-origin-thread"}
            assert "reply_route" not in delivered.metadata
            assert bus.get_queue("channel-web").empty()
        finally:
            await manager.stop()
            await bus.shutdown()

    asyncio.run(run())


def test_progress_notices_deduplicate_only_within_the_same_task_and_thread(agent_db, tmp_path):
    async def run():
        bus = MessageBus(runtime_root=tmp_path)
        manager = ManagerAgent(config=ManagerConfig(work_dir=tmp_path), runtime_root=tmp_path)
        manager._bus = bus
        text = '进度更新: 0/2 完成, 1 执行中'
        changed = '进度更新: 1/2 完成, 1 执行中'
        try:
            for task_id, thread, summary in [('task-a', 'chat-a', text), ('task-a', 'chat-a', text),
                    ('task-b', 'chat-a', text), ('task-a', 'chat-b', text),
                    ('task-a', 'chat-a', changed), ('task-a', 'chat-a', text)]:
                await manager._handle_goal_status(MessageEnvelope.create(
                    source=TASK_ROUTER_ADDRESS, target=manager.agent_address,
                    message_type=MessageType.GOAL_STATUS,
                    payload={'task_id': task_id, 'status': 'executing', 'summary': summary},
                    metadata={'origin_channel_id': 'channel-web', 'origin_thread': thread}))
            queue = bus.get_queue('channel-web')
            messages = [queue.get_nowait().payload for _ in range(queue.qsize())]
            assert messages == [{'text': value, 'chat_session_id': thread} for value, thread in
                [(text, 'chat-a'), (text, 'chat-a'), (text, 'chat-b'), (changed, 'chat-a'), (text, 'chat-a')]]
        finally:
            await manager.stop()
            await bus.shutdown()

    asyncio.run(asyncio.wait_for(run(), 5))


@pytest.mark.parametrize("event,data,expected", [
    (Phase.PLAN_CREATED, {"workflow": {}, "steps": [SimpleNamespace(label="s1", instruction=
        "First\n\t  second " + "completeword " * 10)]}, "First second completeword completeword completeword…"),
    (Phase.ESCALATION, {"step": "s1", "reason": "First\n\t  second " + "completeword " * 30}, "First second"),
    (Phase.CONDITION_BLOCKED, {"step": "s1", "condition": "First\n\t  second " + "completeword " * 30}, "First second"),
    (Phase.VERIFICATION_ROUND, {"step": "s1\n\t  check", "summary": "First\n\t  second " + "completeword " * 30,
        "status": "completed"}, "First second"),
])
def test_phase_notices_flatten_and_bound_inserted_text(event, data, expected):
    text = phase_text(event, data)
    assert expected in text
    assert "\n" not in text and "\t" not in text and "  " not in text
    assert "completewor…" not in text and "completewo…" not in text
    if event is not Phase.PLAN_CREATED:
        assert len(text) < 250 and "…" in text
    assert "completeword…" in text


def test_verification_notice_keeps_summary_across_lines(planner_claude, agent_db, monkeypatch):
    from tests.test_workflow_rules import VERIFY, configure as rules

    rules(agent_db, monkeypatch, phase_notices=True)
    claude = planner_claude(plan(step("s1"), {**step("v1", "s1"), "role": VERIFY}),
        decide("accept"), decide("accept"))
    h = drive(claude, always({**WORK_OK, "changed_files": {}, "summary": "First\n\t second   third",
                             "verification": {"head": "abc123", "git": True}}))
    text = next(e.payload["phase_text"] for e in h.statuses
                if "验证第 1 轮（v1）：通过" in e.payload.get("phase_text", ""))
    assert "First second third" in text


def test_retry_with_running_suggestion_reports_each_round_once(planner_claude, agent_db, monkeypatch):
    configure(agent_db, monkeypatch, phase_notices=True)
    claude = planner_claude(plan(step("s1", review=True)), decide("review"),
        decide("retry", feedback="Handle empty input"), decide("review"), decide("accept"))

    async def scenario(h):
        await h.start()
        await h.until(lambda: len(h.reviews) == 3)
        await h.release(BLOCK)
        await h.release(REVIEW_PASS)
        await h.until(lambda: len(h.reviews) == 6)
        assert len(h.killed) == 1
        for _ in MEMBERS:
            await h.release(REVIEW_PASS)
        assert (await h.finished()).payload["status"] == "completed"
        texts = [e.payload["phase_text"] for e in h.statuses if "phase_text" in e.payload]
        for attempt in (1, 2):
            assert sum(f"评审第 {attempt} 轮" in text for text in texts) == 1
            rounds = [json.loads(e.summary) for e in h.planner._plan.journal if e.kind.value == "review_round"
                      and json.loads(e.summary)["round"] == attempt - 1]
            a, b, c = (f"s1/{member}" for member in MEMBERS)
            # Each arrival is recorded before the single quorum snapshot and notice.
            assert [list(record["reviews"]) for record in rounds] == [[a], [b], [a, b, c]]
            for record, member, result in zip(rounds, (a, b), (BLOCK if attempt == 1 else REVIEW_PASS, REVIEW_PASS)):
                seat = record["reviews"][member]
                assert seat["status"] == "verified"
                assert {key: seat["result"][key] for key in result} == result
            assert rounds[-1]["reviews"] == {
                **rounds[0]["reviews"], **rounds[1]["reviews"], c: {"status": "dispatched", "result": None}}
        late = [e for e in h.planner._plan.journal if e.kind.value == "late_review"]
        assert len(late) == 1 and late[0].step == "s1/review_c"
        assert json.loads(late[0].summary)["passed"] is True

    drive(claude, held_reviews, scenario)
