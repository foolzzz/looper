"""Session bounds follow completion delivery and leave independent task notices intact."""

import asyncio
import json
from datetime import datetime, timedelta, timezone

import pytest

from looper.agents import chat_settings, completion_events, session_budget, turn_metrics
from looper.agents.completion_service import CompletionService
from looper.agents.message import MANAGER_ADDRESS, TASK_ROUTER_ADDRESS, MessageEnvelope, MessageType
from looper.agents.session_switch import SwitchReason
from looper.db import llm_records, projects, tasks
from tests.db_support import DSN, run_db
from tests.manager_budget_support import manager

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")


async def task(pool, root, *, status):
    project = await projects.create_project(name="synthetic", work_dir=str(root), pool=pool)
    await tasks.create_task(task_id="continuing-task", requirement="r", project_id=project["id"],
                            title="Synthetic task", status=status, origin_thread="budget-chat",
                            origin_channel_id="channel-web", pool=pool)


def goal_status():
    return MessageEnvelope.create(
        source=TASK_ROUTER_ADDRESS, target=MANAGER_ADDRESS, message_type=MessageType.GOAL_STATUS,
        payload={"task_id": "continuing-task", "goal_id": "synthetic-goal", "status": "completed",
                 "summary": "Synthetic result"},
        metadata={"origin_thread": "budget-chat", "origin_channel_id": "channel-web",
                  "task_title": "Synthetic task"})


def test_completion_reply_and_maintenance_hold_the_same_session_lock(monkeypatch, tmp_path):
    monkeypatch.setattr(chat_settings, "_completion_turns", chat_settings.CompletionTurns.DM_ONLY)
    monkeypatch.setattr(chat_settings, "_metrics_enabled", True)

    async def check(pool):
        await session_budget.update({"turn_budget_usd": 4})
        await task(pool, tmp_path, status="completed")
        async with manager(pool, tmp_path, monkeypatch, compact="missing") as chat:
            order = []
            send = completion_events.send_reply
            bound = chat.manager._bound_session
            annotate = llm_records.add_metadata

            async def write_metrics(record_id, metadata, **kwargs):
                await annotate(record_id, metadata, **kwargs)
                if "turn_metrics" in metadata:
                    assert chat.manager._turn_lock.locked()
                    order.append("metrics")

            async def send_reply(*args, **kwargs):
                assert chat.manager._turn_lock.locked()
                assert chat.manager._pending_switch is None
                result = await send(*args, **kwargs)
                order.append("reply")
                return result

            async def bound_session(session):
                assert session.turn_lock.locked()
                assert order == ["reply", "metrics"]
                await bound(session)
                order.append("bound")

            monkeypatch.setattr(completion_events, "send_reply", send_reply)
            monkeypatch.setattr(chat.manager, "_bound_session", bound_session)
            monkeypatch.setattr(llm_records, "add_metadata", write_metrics)
            await chat.manager._handle_goal_status(goal_status())
            service = CompletionService(bus=chat.bus, manager_running=lambda _: True,
                                        running_managers=lambda: ["manager"])
            await service.run_once(now=datetime.now(timezone.utc) + timedelta(seconds=61))
            queue = chat.bus.get_queue("manager")
            due = [queue.get_nowait() for _ in range(queue.qsize())]
            assert [event.message_type for event in due] == [MessageType.COMPLETION_DUE]
            await chat.manager._handle_completion_due(due[0])
            assert order == ["reply", "metrics", "bound"]
            assert not chat.manager._turn_lock.locked()
            assert chat.manager._pending_switch.reason is SwitchReason.SESSION_BOUND
            assert not chat.manager._claude.started
            reply = chat.bus.get_queue("channel-web").get_nowait()
            assert reply.payload == {"text": "answer\n\n[/#tasks/continuing-task](/#tasks/continuing-task)",
                                     "chat_session_id": "budget-chat"}
            assert await pool.fetchval("SELECT state FROM manager_events WHERE task_id='continuing-task'") == "delivered"

    run_db(check, monkeypatch, timeout=45)


def test_running_task_delivers_its_end_after_manager_rotation(monkeypatch, tmp_path):
    monkeypatch.setattr(chat_settings, "_completion_turns", chat_settings.CompletionTurns.OFF)

    async def check(pool):
        await session_budget.update({"turn_budget_usd": 4})
        await task(pool, tmp_path, status="running")
        async with manager(pool, tmp_path, monkeypatch, compact="missing") as chat:
            assert await chat.ask() == ["answer"]
            assert chat.manager._pending_switch.reason is SwitchReason.SESSION_BOUND
            assert (await tasks.get_task("continuing-task", pool=pool))["status"] == "running"
            await tasks.update_task_status("continuing-task", "completed", result_summary="Synthetic result", pool=pool)
            await chat.manager._handle_goal_status(goal_status())
            queue = chat.bus.get_queue("channel-web")
            replies = [queue.get_nowait() for _ in range(queue.qsize())]
            assert len(replies) == 1 and replies[0].message_type is MessageType.AGENT_REPLY
            assert replies[0].payload["chat_session_id"] == "budget-chat"
            assert "Synthetic task" in replies[0].payload["text"] and "Synthetic result" in replies[0].payload["text"]
            assert (await tasks.get_task("continuing-task", pool=pool))["status"] == "completed"
            assert not chat.manager._claude.started  # notices do not need to reopen the rotated resident

    run_db(check, monkeypatch, timeout=45)


@pytest.mark.parametrize("budget,native", [(.01, False), (4, True), (None, True)])
def test_completion_cap_preserves_the_session_and_the_default_fallback(monkeypatch, tmp_path, budget, native):
    monkeypatch.setattr(chat_settings, "_completion_turns", chat_settings.CompletionTurns.DM_ONLY)
    monkeypatch.setattr(chat_settings, "_metrics_enabled", True)

    async def check(pool):
        await session_budget.update({"turn_budget_usd": budget})
        await task(pool, tmp_path, status="completed")
        async with manager(pool, tmp_path, monkeypatch, context=20_000, budget_error=native) as chat:
            await chat.manager._handle_goal_status(goal_status())
            service = CompletionService(bus=chat.bus, manager_running=lambda _: True,
                                        running_managers=lambda: ["manager"])
            await service.run_once(now=datetime.now(timezone.utc) + timedelta(seconds=61))
            queue = chat.bus.get_queue("manager")
            due = [queue.get_nowait() for _ in range(queue.qsize())]
            assert [event.message_type for event in due] == [MessageType.COMPLETION_DUE]
            await chat.manager._handle_completion_due(due[0])
            await asyncio.gather(*turn_metrics._pending)
            reply = chat.bus.get_queue("channel-web").get_nowait()
            if budget is None:
                assert "Synthetic result" in reply.payload["text"]
                assert await pool.fetchval("SELECT state FROM manager_events WHERE task_id='continuing-task'") == "fallback"
                metadata = json.loads(await pool.fetchval("SELECT metadata FROM llm_records ORDER BY id DESC LIMIT 1"))
                assert metadata["turn_metrics"]["delivery"] == "over_cap"
            else:
                assert reply.payload == {
                    "text": session_budget.over_cap_reply(budget) + "\n\n[/#tasks/continuing-task](/#tasks/continuing-task)",
                    "chat_session_id": "budget-chat"}
                assert await pool.fetchval("SELECT state FROM manager_events WHERE task_id='continuing-task'") == "delivered"
            assert not chat.manager._claude.started
            assert chat.manager._session_now().session_id
            assert await pool.fetchval("SELECT session_id FROM agent_sessions WHERE agent_id='manager'")
            assert chat.manager._pending_switch is None

    run_db(check, monkeypatch, timeout=45)
