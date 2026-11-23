"""The Planner's review entry points share measured evidence and honor its gate."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from looper.agents import acceptance_runner
from looper.agents.acceptance_checks import CommandCheck
from looper.agents.message import MessageType
from looper.agents.project_execution import AcceptanceChecksMode, ProjectExecution
from looper.agents.task_roles import RoleKind, TaskRole
from looper.agents.tiers import planner, planner_protocol as protocol
from looper.db import step_check_runs


def review_plan(tmp_path, monkeypatch, *, passed=True, review=True):
    agent = planner.PlannerAgent(task_id="task-evidence", runtime_root=tmp_path / "runtime")
    agent._execution = ProjectExecution(acceptance_checks=AcceptanceChecksMode.NETWORK)
    agent._bus = SimpleNamespace(publish=AsyncMock())
    target = planner.PlanStep("work-step", 0, "Implement API", key="s1", review=review,
        acceptance_criteria=["API returns the result", "The naming is clear"],
        checks=(CommandCheck("api", 1, "python test_api.py", network=True),),
        status="completed", result={"status": "completed"})
    seats = [planner.PlanStep(f"seat-{key}", index + 1, "Review API", key=f"s1/{key}",
                             reviews=target.step_id, role=key) for index, key in enumerate(("review_a", "review_b"))]
    plan = planner.TaskPlan("plan-evidence", "goal-evidence", "Deliver API", str(tmp_path),
                            [target, *seats], status="executing")
    agent._plan = plan
    history = [{"step_id": target.step_id, "check_id": "api", "attempt": 1, "run": 1,
                "state": "passed" if passed else "failed", "head_sha": None, "content_sha": None,
                "exit_code": 0 if passed else 1, "duration_ms": 5, "output_tail": "API output",
                "protected_changed": [], "error_kind": None}]
    monkeypatch.setattr(step_check_runs, "list_runs", AsyncMock(side_effect=lambda *a, **k: list(history)))
    monkeypatch.setattr(acceptance_runner, "run_step_checks", AsyncMock(side_effect=lambda **k: list(history)))
    monkeypatch.setattr(agent, "_save", AsyncMock())
    monkeypatch.setattr(agent, "_activity", AsyncMock())
    monkeypatch.setattr(agent, "_within_budget", AsyncMock(return_value=True))
    monkeypatch.setattr(agent, "_role", AsyncMock(side_effect=lambda key, *a:
        TaskRole(key, RoleKind.REVIEW, 1, "Review API correctness.")))
    return agent, plan, target, seats


def test_builtin_and_every_panel_seat_get_the_same_evidence(tmp_path, monkeypatch):
    async def run():
        agent, plan, target, seats = review_plan(tmp_path, monkeypatch)
        await agent._spawn_reviewer(plan, target)
        for seat in seats:
            await agent._dispatch_review(plan, seat)
        envelopes = [call.args[0] for call in agent._bus.publish.await_args_list]
        assert len(envelopes) == 1 + len(seats)
        assert all(envelope.message_type is MessageType.VERIFY_REQUEST for envelope in envelopes)
        sections = [envelope.payload["evidence"] for envelope in envelopes]
        assert len(set(sections)) == 1
        assert "api command: PASSED" in sections[0]
        assert "criterion 2" in sections[0] and "no check" in sections[0]
        assert "output of the code under test" in sections[0]
        assert {envelope.payload["task_role"]["id"] for envelope in envelopes[1:]} == {seat.role for seat in seats}
        assert all(envelope.payload["severity"] is True for envelope in envelopes[1:])
    asyncio.run(run())


@pytest.mark.parametrize("panel", [False, True], ids=["builtin", "panel-seat"])
def test_failed_evidence_returns_to_a_decision_without_dispatch(tmp_path, monkeypatch, panel):
    async def run():
        agent, plan, target, seats = review_plan(tmp_path, monkeypatch, passed=False)
        monkeypatch.setattr(agent, "_decide_again", AsyncMock())
        if panel:
            await agent._dispatch_review(plan, seats[0])
        else:
            await agent._spawn_reviewer(plan, target)
        agent._bus.publish.assert_not_awaited()
        agent._decide_again.assert_awaited_once_with(plan, target)
        assert target.result["acceptance"]["blocked"] is True
        assert target.reviewer_id is None
        assert all(seat.reviewer_id is None for seat in seats)
    asyncio.run(run())


@pytest.mark.parametrize("passed", [False, True], ids=["required-failed", "required-passed"])
def test_review_false_uses_the_gate_without_a_reviewer(tmp_path, monkeypatch, passed):
    async def run():
        agent, plan, target, _ = review_plan(tmp_path, monkeypatch, passed=passed, review=False)
        # No other open step is needed for this work. A missing model decision uses its measured result.
        plan.steps = [target]
        monkeypatch.setattr(agent, "_room_lines", AsyncMock(return_value=[]))
        monkeypatch.setattr(agent, "_asking", AsyncMock(return_value=lambda text: text))
        monkeypatch.setattr(agent, "_ask", AsyncMock(side_effect=planner.PlannerTurnError("invalid decision")))
        monkeypatch.setattr(agent, "_merged", AsyncMock(return_value=True))
        monkeypatch.setattr(agent, "_step_settled", AsyncMock())
        monkeypatch.setattr(agent, "_send_to_review", AsyncMock())
        context = protocol.DecisionContext(target.label, protocol.ResultKind.WORK, True, False, 0)
        await agent._decide(plan, target, context, ["Worker completed"])
        assert target.status == ("verified" if passed else "failed")
        offered = agent._ask.await_args.args[1].split("Allowed decisions now: ", 1)[1].splitlines()[0]
        assert ("accept" in offered) is passed
        agent._send_to_review.assert_not_awaited()
        agent._bus.publish.assert_not_awaited()
    asyncio.run(run())


def test_source_change_between_panel_seats_invalidates_the_round_before_dispatch(tmp_path, monkeypatch):
    import subprocess

    from looper.agents import acceptance_decisions

    def git(directory, *args):
        return subprocess.run(["git", "-C", str(directory), *args], check=True, capture_output=True,
                              text=True, timeout=10).stdout.strip()

    async def run():
        agent, plan, target, seats = review_plan(tmp_path, monkeypatch)
        source = tmp_path / "source"
        source.mkdir()
        git(source, "init", "-q")
        git(source, "config", "user.name", "Example")
        git(source, "config", "user.email", "dev@example.org")
        (source / "api.py").write_text("RESULT = 1\n")
        git(source, "add", "api.py")
        git(source, "commit", "-qm", "Initial API")
        plan.work_dir = str(source)
        original = await acceptance_decisions._head(plan, target)
        target.result["head"] = original[0]
        target.accept_when = {"all_of": [seat.step_id for seat in seats]}
        history = [{"step_id": target.step_id, "check_id": "api", "attempt": 1, "run": 1,
                    "state": "passed", "head_sha": original[0], "content_sha": original[1],
                    "exit_code": 0, "duration_ms": 1, "output_tail": "API checked", "error_kind": None}]
        monkeypatch.setattr(step_check_runs, "list_runs", AsyncMock(side_effect=lambda *a, **k: list(history)))

        async def collect(**kwargs):
            if kwargs.get("rerun_after"):
                head, content = await acceptance_decisions._head(plan, target)
                history.append({**history[-1], "run": history[-1]["run"] + 1,
                                "state": "passed", "head_sha": head, "content_sha": content})
            return list(history)

        async def mark_stale(**kwargs):
            history.append({**history[-1], "run": history[-1]["run"] + 1, "state": "stale"})

        async def open_workspace(task, seat, base):
            directory = tmp_path / seat.step_id
            git(tmp_path, "clone", "-q", "--shared", "--no-checkout", str(source), str(directory))
            git(directory, "checkout", "-q", "--detach", base.commit)
            seat.workspace = SimpleNamespace(work_dir=str(directory))

        monkeypatch.setattr(acceptance_runner, "run_step_checks", AsyncMock(side_effect=collect))
        monkeypatch.setattr(acceptance_runner, "mark_stale", AsyncMock(side_effect=mark_stale))
        monkeypatch.setattr(agent, "_open_workspace", open_workspace)
        monkeypatch.setattr(agent, "_kill_worker", AsyncMock())
        monkeypatch.setattr(agent, "_discard", AsyncMock(return_value=None))
        monkeypatch.setattr(agent, "_decide_again", AsyncMock())
        monkeypatch.setattr(agent, "_report_progress", AsyncMock())
        await agent._dispatch_review(plan, seats[0])
        first = agent._bus.publish.await_args.args[0]
        first_reviewer = seats[0].reviewer_id
        first_workspace = seats[0].workspace
        assert git(first_workspace.work_dir, "rev-parse", "HEAD") == first.payload["head"]
        assert "PASSED" in first.payload["evidence"]
        assert agent._bus.publish.await_count == 1
        target.result.update(verify_passed=True, role_reviews=[{"passed": True}], review_round=target.retries)
        seats[1].result = {"passed": True, "findings": []}
        (source / "api.py").write_text("RESULT = 2\n")
        git(source, "add", "api.py")
        git(source, "commit", "-qm", "New API result")
        await agent._dispatch_review(plan, seats[1])
        assert agent._bus.publish.await_count == 1  # No seat receives an old checkout with new evidence.
        agent._decide_again.assert_awaited_once_with(plan, target)
        agent._kill_worker.assert_awaited_once_with(seats[0], run_id=first_reviewer)
        agent._discard.assert_awaited_once_with(first_workspace)
        assert all(seat.status == "pending" and seat.result is None and seat.workspace is None for seat in seats)
        assert all(seat.worker_id is None and seat.reviewer_id is None for seat in seats)
        assert not {"worker_head", "verify_passed", "role_reviews", "review_round"} & target.result.keys()
        assert target.result["acceptance"]["stale_reruns_used"] == 1
        assert history[-1]["state"] == "passed" and history[-1]["head_sha"] != original[0]
        assert (source / "api.py").read_text() == "RESULT = 2\n"
    asyncio.run(run())
