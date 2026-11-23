"""Manager cost admission through a stand-in resident, real records and completion fallback."""

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest

from looper.agents import chat_settings, cost_guard as cg, cost_manager, session_budget
from looper.agents.completion_service import CompletionService
from looper.agents.cost_guard_settings import Config, Mode
from looper.agents.message import MANAGER_ADDRESS, TASK_ROUTER_ADDRESS, MessageEnvelope, MessageType, channel_address
from looper.agents.ops.service import ManagerOps, Platform
from looper.db import projects, tasks
from tests.db_support import DSN, run_db
from tests.manager_budget_support import manager

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")


def configure(chat, pool, monkeypatch):
    guard = cg.Guard(Config(mode=Mode.ENFORCE, daily_usd=0, daily_equivalent_usd=0), pool=pool)
    guard.day = guard.day_start(guard.clock())
    monkeypatch.setattr(cg, "_guard", guard)
    chat.manager.ops = ManagerOps(Platform(bus=chat.bus, task_router=None), url="http://127.0.0.1:9/")
    monkeypatch.setattr(chat.manager, "_tools_problem", AsyncMock(return_value=None))


def test_operator_replies_once_with_notice_and_maintenance_waits_without_prompt_changes(monkeypatch, tmp_path):
    async def check(pool):
        await session_budget.update({"turn_budget_usd": 4})
        async with manager(pool, tmp_path, monkeypatch, context=100_001) as chat:
            configure(chat, pool, monkeypatch)
            assert await chat.ask("synthetic first") == [
                "answer\n\n今天的花费已经到了上限（$0.00），后台任务已暂停"]
            assert await chat.ask("synthetic second") == ["answer"]
            inputs = [row["input_text"] for row in await pool.fetch("SELECT input_text FROM llm_records ORDER BY id")]
            assert len(inputs) == 2
            assert all("今天的花费" not in text and "/compact" not in text for text in inputs)
            assert chat.manager._pending_switch is None
            assert chat.manager._claude.started
    run_db(check, monkeypatch, timeout=45)


def test_api_client_turn_is_denied_without_starting_a_model(monkeypatch, tmp_path):
    async def check(pool):
        async with manager(pool, tmp_path, monkeypatch, context=20_000) as chat:
            configure(chat, pool, monkeypatch)
            envelope = MessageEnvelope.create(
                source=channel_address("web"), target=MANAGER_ADDRESS, message_type=MessageType.USER_MESSAGE,
                payload={"text": "synthetic client", "channel_context": {
                    "session_id": "budget-chat", "client": {"id": "synthetic-client", "name": "Synthetic"}}},
                metadata={"origin_channel_id": "channel-web"})
            await chat.manager._handle_user_message(envelope)
            queue = chat.bus.get_queue("channel-web")
            replies = [item.payload["text"] for item in [queue.get_nowait() for _ in range(queue.qsize())]
                       if item.message_type is MessageType.AGENT_REPLY]
            assert replies == [cost_manager.DENIAL_REPLY]
            assert await pool.fetchval("SELECT count(*) FROM llm_records") == 0
            assert not (tmp_path / "inputs.jsonl").exists()
    run_db(check, monkeypatch, timeout=45)


def test_completion_over_global_cap_uses_existing_code_fallback(monkeypatch, tmp_path):
    monkeypatch.setattr(chat_settings, "_completion_turns", chat_settings.CompletionTurns.DM_ONLY)

    async def check(pool):
        project = await projects.create_project(name="synthetic", work_dir=str(tmp_path), pool=pool)
        await tasks.create_task(task_id="synthetic-completed", requirement="r", project_id=project["id"],
                                title="Synthetic task", status="completed", origin_thread="budget-chat",
                                origin_channel_id="channel-web", pool=pool)
        async with manager(pool, tmp_path, monkeypatch, context=20_000) as chat:
            configure(chat, pool, monkeypatch)
            goal = MessageEnvelope.create(
                source=TASK_ROUTER_ADDRESS, target=MANAGER_ADDRESS, message_type=MessageType.GOAL_STATUS,
                payload={"task_id": "synthetic-completed", "goal_id": "synthetic-goal", "status": "completed",
                         "summary": "Synthetic result"},
                metadata={"origin_thread": "budget-chat", "origin_channel_id": "channel-web",
                          "task_title": "Synthetic task"})
            await chat.manager._handle_goal_status(goal)
            service = CompletionService(bus=chat.bus, manager_running=lambda _: True,
                                        running_managers=lambda: ["manager"])
            await service.run_once(now=datetime.now(timezone.utc) + timedelta(seconds=61))
            due = chat.bus.get_queue("manager").get_nowait()
            assert due.message_type is MessageType.COMPLETION_DUE
            await chat.manager._handle_completion_due(due)
            reply = chat.bus.get_queue("channel-web").get_nowait()
            assert "Synthetic result" in reply.payload["text"]
            assert await pool.fetchval("SELECT state FROM manager_events WHERE task_id='synthetic-completed'") == "fallback"
            assert await pool.fetchval("SELECT count(*) FROM llm_records") == 0
            assert not (tmp_path / "inputs.jsonl").exists()
    run_db(check, monkeypatch, timeout=45)


def test_selected_provider_still_changes_when_the_old_handoff_is_denied(monkeypatch, tmp_path):
    from dataclasses import replace
    import json

    async def check(pool):
        async with manager(pool, tmp_path, monkeypatch, context=20_000) as chat:
            configure(chat, pool, monkeypatch)
            await chat.ask("synthetic first")
            settings = await chat.manager._load_agent_settings(fallback=False)
            resolved = settings.profile.resolved_model
            provider = replace(resolved.provider, id="synthetic-provider")
            resolved = replace(resolved, provider=provider,
                               model=replace(resolved.model, provider_id=provider.id),
                               endpoint=replace(resolved.endpoint, provider_id=provider.id))
            profile = replace(settings.profile, id="synthetic-selected-profile", resolved_model=resolved)
            monkeypatch.setattr(chat.manager, "_load_agent_settings", AsyncMock(return_value=replace(settings, profile=profile)))
            assert await chat.ask("synthetic selected provider") == ["answer"]
            rows = await pool.fetch("SELECT input_text, metadata FROM llm_records ORDER BY id")
            assert len(rows) == 2
            assert all(not row["input_text"].startswith("[HANDOFF]") for row in rows)
            metadata = json.loads(rows[-1]["metadata"])
            assert metadata["profile"]["id"] == "synthetic-selected-profile"
            assert metadata["llm_model"]["provider_id"] == "synthetic-provider"
            assert chat.manager._session_provider == "synthetic-provider"
    run_db(check, monkeypatch, timeout=45)


def test_pending_rotation_is_retained_over_cap_and_retried_after_the_cap_is_raised(monkeypatch, tmp_path):
    from looper.agents.session_switch import SessionSwitch, SwitchReason

    async def check(pool):
        async with manager(pool, tmp_path, monkeypatch, context=20_000) as chat:
            configure(chat, pool, monkeypatch)
            await chat.ask("synthetic first")
            session = chat.manager._session_now()
            session_id = session.session_id
            pending = SessionSwitch(SwitchReason.SESSION_BOUND, "synthetic maintenance")
            session.pending_switch = pending
            assert await chat.ask("synthetic second") == ["answer"]
            assert session.session_id == session_id and session.pending_switch is pending
            assert await pool.fetchval("SELECT count(*) FROM llm_records") == 2
            cg.current().config = Config(mode=Mode.ENFORCE, daily_usd=10, daily_equivalent_usd=10)
            assert await chat.ask("synthetic third") == ["answer"]
            assert session.pending_switch is None
            inputs = [row["input_text"] for row in await pool.fetch("SELECT input_text FROM llm_records ORDER BY id")]
            assert len(inputs) == 4
            assert inputs[2].startswith("[HANDOFF]")
    run_db(check, monkeypatch, timeout=45)
