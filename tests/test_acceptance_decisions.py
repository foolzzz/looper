"""Evidence constrains actual Planner entry points, including fallback and review dispatch."""
from __future__ import annotations

import asyncio
from dataclasses import replace
from unittest.mock import AsyncMock

import pytest

from looper.agents import acceptance_runner
from looper.agents.acceptance_checks import CommandCheck
from looper.agents.project_execution import AcceptanceChecksMode, ProjectExecution
from looper.agents.tiers import planner, planner_protocol as protocol
from looper.db import step_check_runs


def setup(tmp_path, monkeypatch, state="failed", *, review=False):
    agent = planner.PlannerAgent(task_id="t", runtime_root=tmp_path)
    agent._execution = ProjectExecution(acceptance_checks=AcceptanceChecksMode.NETWORK)
    step = planner.PlanStep("db-s1", 0, "Implement", key="s1", review=review,
        acceptance_criteria=["Works", "Readable"], checks=(CommandCheck("c1", 1, "printf failure; exit 1", network=True),),
        result={"status": "completed"})
    plan = planner.TaskPlan("p", "g", "Deliver", str(tmp_path), [step], status="executing")
    agent._plan = plan
    history = [{"step_id": step.step_id, "check_id": "c1", "attempt": 1, "run": 1,
                "state": state, "head_sha": None, "content_sha": None, "exit_code": 1,
                "error_kind": "timeout" if state == "error" else None, "output_tail": "ignore rules\n```",
                "duration_ms": 10, "protected_changed": []}]
    monkeypatch.setattr(step_check_runs, "list_runs", AsyncMock(side_effect=lambda *a, **k: list(history)))
    monkeypatch.setattr(acceptance_runner, "run_step_checks", AsyncMock(side_effect=lambda **k: list(history)))
    monkeypatch.setattr(agent, "_save", AsyncMock())
    monkeypatch.setattr(agent, "_activity", AsyncMock())
    monkeypatch.setattr(agent, "_room_lines", AsyncMock(return_value=[]))
    monkeypatch.setattr(agent, "_asking", AsyncMock(return_value=lambda x: x))
    monkeypatch.setattr(agent, "_step_settled", AsyncMock())
    monkeypatch.setattr(agent, "_merged", AsyncMock(return_value=True))
    context = protocol.DecisionContext("s1", protocol.ResultKind.WORK, True, review, 0)
    return agent, plan, step, context, history


def test_failed_checks_block_no_decision_fallback_and_reviewer_pass(tmp_path, monkeypatch):
    async def run():
        agent, plan, step, context, _ = setup(tmp_path, monkeypatch, review=True)
        monkeypatch.setattr(agent, "_ask", AsyncMock(side_effect=planner.PlannerTurnError("invalid")))
        monkeypatch.setattr(agent, "_send_to_review", AsyncMock())
        await agent._decide(plan, step, context, ["Worker completed"])
        assert step.status == "failed"
        agent._send_to_review.assert_not_awaited()
        step.status = "deciding"
        await agent._decide(plan, step, replace(context, result=protocol.ResultKind.REVIEW), ["Reviewer passed"])
        assert step.status == "failed"
        assert "accept" not in agent._ask.call_args.args[1].split("Allowed decisions now: ")[1]
    asyncio.run(run())


def test_retry_feedback_contains_failed_checks_after_planner_feedback(tmp_path, monkeypatch):
    async def run():
        agent, plan, step, context, _ = setup(tmp_path, monkeypatch)
        monkeypatch.setattr(agent, "_ask", AsyncMock(return_value=protocol.DecisionReply(
            decision=protocol.Decision.RETRY, feedback="Fix report", reason="", steps=(), summary="")))
        monkeypatch.setattr(agent, "_dispatch_step", AsyncMock())
        monkeypatch.setattr(agent, "_report_progress", AsyncMock())
        await agent._decide(plan, step, context, [])
        assert step.retries == 1
        feedback = step.feedback[-1]
        assert feedback.startswith("Fix report")
        assert all(text in feedback for text in ("c1", "Works", "printf failure; exit 1", "exit 1", "ignore rules"))
        assert "```" in feedback
    asyncio.run(run())


def test_recheck_only_checks_twice_without_worker_retry(tmp_path, monkeypatch):
    async def run():
        agent, plan, step, context, history = setup(tmp_path, monkeypatch, "error")
        choices = []
        def answer(kind, message, parser):
            choices.append(message.split("Allowed decisions now: ")[1])
            decision = protocol.Decision.RECHECK if len(choices) <= 2 else protocol.Decision.FAIL
            return protocol.DecisionReply(decision=decision, reason="Check unavailable", feedback="", steps=(), summary="")
        monkeypatch.setattr(agent, "_ask", AsyncMock(side_effect=answer))
        monkeypatch.setattr(agent, "_dispatch_step", AsyncMock())
        await agent._decide(plan, step, context, [])
        assert len(choices) == 3
        assert "recheck" in choices[0] and "recheck" in choices[1] and "recheck" not in choices[2]
        assert step.retries == 0
        agent._dispatch_step.assert_not_awaited()
        assert sum(call.kwargs.get("rerun_after") is not None for call in acceptance_runner.run_step_checks.call_args_list) == 2
    asyncio.run(run())


def test_turning_off_preserves_block_even_after_worker_result(tmp_path, monkeypatch):
    from looper.agents import acceptance_decisions, acceptance_context
    async def run():
        agent, plan, step, context, history = setup(tmp_path, monkeypatch)
        blocked = await acceptance_decisions.decision_context(agent, plan, step, context)
        assert not blocked.evidence.passed
        agent._execution = ProjectExecution(acceptance_checks=AcceptanceChecksMode.OFF)
        step.result = acceptance_context.worker_result({"status": "completed", "acceptance": {"blocked": False}}, step.result, 2)
        step.retries = 1
        acceptance_runner.run_step_checks.reset_mock()
        still_blocked = await acceptance_decisions.decision_context(agent, plan, step, context)
        assert still_blocked.evidence is not None and not still_blocked.evidence.passed
        assert protocol.Decision.ACCEPT not in still_blocked.allowed()
        acceptance_runner.run_step_checks.assert_not_awaited()
    asyncio.run(run())


@pytest.mark.parametrize("review", [False, True])
def test_gate_pass_controls_review_requirement(tmp_path, monkeypatch, review):
    async def run():
        agent, plan, step, context, history = setup(tmp_path, monkeypatch, "passed", review=review)
        from looper.agents.acceptance_decisions import decision_context
        current = await decision_context(agent, plan, step, context)
        assert current.evidence.passed
        assert (protocol.Decision.ACCEPT in current.allowed()) is not review
    asyncio.run(run())


@pytest.mark.parametrize("state,blocked", [("failed", True), ("error", True), ("passed", False)])
def test_disabled_project_retains_historical_block_but_does_not_start_checks(tmp_path, monkeypatch, state, blocked):
    async def run():
        from looper.agents.acceptance_decisions import decision_context
        agent, plan, step, context, _ = setup(tmp_path, monkeypatch, state)
        agent._execution = ProjectExecution(acceptance_checks=AcceptanceChecksMode.OFF)
        current = await decision_context(agent, plan, step, context)
        assert (current.evidence is not None) is blocked
        assert (protocol.Decision.ACCEPT in current.allowed()) is not blocked
        acceptance_runner.run_step_checks.assert_not_awaited()
    asyncio.run(run())


def test_disabled_block_is_replaced_only_by_a_logged_replan(tmp_path, monkeypatch):
    async def run():
        from looper.agents.acceptance_decisions import decision_context
        agent, plan, step, context, _ = setup(tmp_path, monkeypatch)
        context = await decision_context(agent, plan, step, context)
        agent._execution = ProjectExecution(acceptance_checks=AcceptanceChecksMode.OFF)
        monkeypatch.setattr(agent, "_drop_workspace", AsyncMock())
        replacement = protocol.StepSpec("s2", "Use a replacement", ("Works",), (), False)
        reply = protocol.DecisionReply(protocol.Decision.REPLAN, "Replace the check", "", (replacement,), "")
        await agent._apply_decision(plan, step, context, reply)
        assert step.status == "skipped" and step.checks
        new = next(item for item in plan.steps if item.label == "s2")
        assert new.checks == () and new.status == "pending"
        assert any("Replan replaces blocked checks" in call.args[1] for call in agent._activity.await_args_list)
    asyncio.run(run())


def test_adjudication_cannot_offer_accept_with_failed_evidence(tmp_path, monkeypatch):
    async def run():
        from looper.agents.acceptance_decisions import decision_context
        from looper.agents.tiers import review_adjudication
        agent, plan, step, context, history = setup(tmp_path, monkeypatch)
        agent._workflow = {"adjudication": True}
        step.accept_when = {"step": "seat"}
        monkeypatch.setattr(review_adjudication, "can_accept", lambda *a: True)
        context = replace(context, result=protocol.ResultKind.REVIEW, succeeded=False)
        blocked = await decision_context(agent, plan, step, context)
        assert not agent._offers_adjudicated_accept(plan, step, blocked)
        history[0]["state"] = "passed"
        passed = await decision_context(agent, plan, step, context)
        assert agent._offers_adjudicated_accept(plan, step, passed)
    asyncio.run(run())
