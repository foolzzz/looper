"""The real run stream reaches the typed result boundary before review aggregation."""
import asyncio
import json
from dataclasses import replace
from unittest.mock import AsyncMock

import pytest

from looper.agents import failures
from looper.agents.message import MessageEnvelope, MessageType, planner_address, worker_address
from looper.agents.tiers import planner
from tests.run_handle_support import ScriptedRun
from test_worker_run_handles import _pool, _finish


@pytest.mark.parametrize("mode", ["off", "record", "act"])
@pytest.mark.parametrize("cause", ["connection_failed", "usage_limit_reached", None])
def test_worker_stream_carries_control_signal_only_in_act(tmp_path, monkeypatch, agent_db, mode, cause):
    agent_db.get_settings.return_value = {failures.SETTING_KEY: mode}
    event = ({"type": "result", "subtype": "error_during_execution", "is_error": True,
              "api_error": cause, "result": "runtime could not complete"} if cause else
             {"type": "result", "subtype": "success", "result": json.dumps({
                 "status": "completed", "summary": "done", "changed_files": {}})})

    async def run():
        async with _pool(tmp_path, monkeypatch, ScriptedRun([event])) as (pool, _, results):
            await _finish(pool, tmp_path)
            payload = results[0].payload
            if mode != "act":
                assert "engine_run" not in payload
                return
            signal = payload["engine_run"]
            assert signal["record_id"] is not None
            assert signal["provider_id"] == "prov-claude-subscription"
            assert signal["outcome"] == ("run_failed" if cause else "completed")
            if cause:
                assert signal["failure"]["cause"] == {"usage_limit_reached": "usage_limit"}.get(cause, cause)
            else:
                assert signal["failure"] is None
    asyncio.run(run())


def test_hold_blocks_dispatch_without_blocking_sibling():
    held = planner.PlanStep("held", 0, "recover")
    held.failure_hold = {"kind": "environment", "failure_id": 1}
    sibling = planner.PlanStep("other", 1, "independent")
    plan = planner.TaskPlan("plan", "goal", "work", "/tmp", [held, sibling], status="executing")
    assert plan.ready_steps() == [sibling]
    assert not held.started
    assert planner._reported_status(plan) == "waiting_user"


@pytest.mark.parametrize("review", [False, True])
def test_current_result_routes_before_decision_or_review_verdict(tmp_path, monkeypatch, agent_db, review):
    async def run():
        agent = planner.PlannerAgent(task_id="task", runtime_root=tmp_path)
        step = planner.PlanStep("step", 0, "work", worker_id="worker", reviewer_id="reviewer",
                                status="completed" if review else "dispatched")
        agent._plan = planner.TaskPlan("plan", "goal", "work", str(tmp_path), [step], status="executing")
        router = AsyncMock(return_value=True)
        monkeypatch.setattr(agent, "_handle_engine_result", router)
        decision = AsyncMock()
        monkeypatch.setattr(agent, "_decide", decision)
        envelope = MessageEnvelope.create(source=worker_address("reviewer" if review else "worker"),
            target=planner_address("task"), message_type=MessageType.VERIFY_RESULT if review else MessageType.WORK_RESULT,
            payload={"task_id": "step", "status": "failed", "passed": False, "engine_run": {"outcome": "run_failed"}})
        await (agent._handle_verify_result(envelope) if review else agent._handle_work_result(envelope))
        router.assert_awaited_once()
        decision.assert_not_awaited()
        envelope = replace(envelope, source=worker_address("old-run"))
        router.reset_mock()
        await (agent._handle_verify_result(envelope) if review else agent._handle_work_result(envelope))
        router.assert_not_awaited()
    asyncio.run(run())


def test_repeated_not_passed_stops_at_existing_limit(planner_claude, agent_db):
    from test_planner_agent import plan, step, decide, drive, always, WORK_OK, REVIEW_FAIL
    claude = planner_claude(plan(step("s1", review=True)), decide("review"),
        decide("retry", feedback="repair"), decide("review"),
        decide("retry", feedback="repair"), decide("review"), decide("retry", feedback="again"))
    h = drive(claude, always(WORK_OK, REVIEW_FAIL))
    assert len(h.work_items) == 3
    assert len(h.reviews) == 3
    assert h.final().payload["status"] == "failed"


def test_scheduled_first_frozen_admission_expires_without_starting_a_run(tmp_path, monkeypatch, agent_db):
    from contextlib import asynccontextmanager
    from datetime import datetime, timedelta, timezone
    from looper.agents import admission, failure_handling, turn_admission
    from looper.db import tasks

    agent_db.get_settings.return_value = {failures.SETTING_KEY: "act"}
    monkeypatch.setattr(tasks, "get_task", AsyncMock(return_value={"schedule_id": "synthetic-schedule"}))
    monkeypatch.setattr(turn_admission, "scheduled_quota_deadline", AsyncMock(
        return_value=datetime.now(timezone.utc) - timedelta(seconds=1)))

    @asynccontextmanager
    async def slot(*args, on_frozen, **kwargs):
        await on_frozen(None)
        await asyncio.Future()
        yield

    async def scenario():
        service = failure_handling.FailureHandlingService(mode=failures.FailureTyping.ACT)
        monkeypatch.setattr(failure_handling, "_service", service)
        monkeypatch.setattr(admission, "slot", slot)
        run = ScriptedRun([])
        try:
            async with _pool(tmp_path, monkeypatch, run) as (pool, _, results):
                await _finish(pool, tmp_path)
                assert run.launch_calls == 0
                agent_db.start_record.assert_not_awaited()
                assert results[0].payload["engine_run"]["quota_exhausted"] is True
                assert not pool._slots
        finally:
            await service.close()
    asyncio.run(asyncio.wait_for(scenario(), 5))


@pytest.mark.parametrize("reader", ["planner", "worker", "lessons"])
def test_failure_hold_audit_reaches_existing_model_context_readers(reader):
    from looper.agents.tiers import journal
    entry = journal.JournalEntry("step", "work", "Failure 42: operator released hold with retry.",
                                 kind=journal.JournalKind.FAILURE_HOLD)
    if reader == "worker":
        text = journal.worker_section([entry], [])
    else:
        lines = journal.planner_section([entry]) if reader == "planner" else journal.lessons_journal([entry])
        text = "\n".join(lines)
    assert "Failure 42" in text and "operator released hold with retry" in text
