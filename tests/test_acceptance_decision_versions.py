"""Version changes and resumed reruns exercise the evidence decision boundary."""
from __future__ import annotations

import asyncio
from copy import deepcopy
from unittest.mock import AsyncMock

import pytest

from looper.agents import acceptance_decisions as decisions, acceptance_runner as runner
from looper.agents.acceptance_checks import CommandCheck
from looper.agents.acceptance_gate import current_evidence
from looper.agents.project_execution import AcceptanceChecksMode, ProjectExecution
from looper.agents.tiers import planner, planner_protocol as protocol
from looper.agents.tiers.worker import _verification_snapshot
from looper.db import step_check_runs
from tests.db_support import run_db
from tests.test_step_workspaces import git


def repository(tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    git(work, "init")
    (work / "code.txt").write_text("original\n")
    commit(work)
    return work


def commit(work):
    git(work, "add", "code.txt")
    git(work, "-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-m", "change")


def setup(tmp_path, monkeypatch):
    work = repository(tmp_path)
    agent = planner.PlannerAgent(task_id="task-versions", runtime_root=tmp_path / "runtime")
    agent._execution = ProjectExecution(acceptance_checks=AcceptanceChecksMode.NETWORK)
    step = planner.PlanStep("step-versions", 0, "Implement", key="s1", status="deciding", review=False,
        acceptance_criteria=["works"], checks=(CommandCheck("c1", 1, "printf verified", network=True),),
        result={"status": "completed"})
    plan = planner.TaskPlan("plan-versions", "goal", "Implement", str(work), [step], status="executing")
    agent._plan = plan
    monkeypatch.setattr(agent, "_save", AsyncMock())
    monkeypatch.setattr(agent, "_activity", AsyncMock())
    context = protocol.DecisionContext("s1", protocol.ResultKind.WORK, True, False, 0)
    return agent, plan, step, context, work


def memory_measurements(agent, plan, step, monkeypatch):
    history = []

    def append(state):
        snapshot = _verification_snapshot(str(plan.work_dir))
        row = {"step_id": step.step_id, "check_id": "c1", "attempt": step.retries + 1,
               "run": max((row["run"] for row in history), default=0) + 1,
               "state": state, "head_sha": snapshot.get("head"),
               "content_sha": snapshot.get("content_sha256"), "error_kind": None,
               "exit_code": 0, "duration_ms": 1, "output_tail": "verified", "protected_changed": []}
        history.append(row)
        return row

    async def collect(**kwargs):
        previous = history[-1] if history else None
        after = kwargs.get("rerun_after")
        if previous is None or after is not None and previous["run"] <= after.get("c1", 0):
            append("passed")
        return list(history)

    async def stale(**kwargs):
        previous = history[-1]
        history.append({**previous, "state": "stale", "run": previous["run"] + 1})
        return [history[-1]]

    monkeypatch.setattr(step_check_runs, "list_runs", AsyncMock(side_effect=lambda *a, **k: list(history)))
    monkeypatch.setattr(runner, "run_step_checks", AsyncMock(side_effect=collect))
    monkeypatch.setattr(runner, "mark_stale", AsyncMock(side_effect=stale))
    return history, append


@pytest.mark.parametrize("committed", [False, True], ids=["dirty-content", "new-head"])
def test_real_version_change_appends_stale_then_measures_the_new_version(tmp_path, monkeypatch, committed):
    async def run():
        agent, plan, step, context, work = setup(tmp_path, monkeypatch)
        history, _ = memory_measurements(agent, plan, step, monkeypatch)
        before = await decisions.decision_context(agent, plan, step, context)
        original = deepcopy(history[0])
        (work / "code.txt").write_text("changed\n")
        if committed:
            commit(work)
        after = await decisions.decision_context(agent, plan, step, context)
        assert after.evidence.passed and after.evidence.stale_reruns_used == 1
        assert [row["state"] for row in history] == ["passed", "stale", "passed"]
        assert [row["run"] for row in history] == [1, 2, 3]
        assert history[0] == original
        assert after.evidence.head[1] != before.evidence.head[1]
        assert (after.evidence.head[0] != before.evidence.head[0]) is committed
        assert all(row["attempt"] == 1 for row in history)
        assert step.retries == 0
    asyncio.run(run())


def test_two_stale_reruns_are_separate_from_rechecks_and_then_leave_the_gate_closed(tmp_path, monkeypatch):
    async def run():
        agent, plan, step, context, work = setup(tmp_path, monkeypatch)
        history, _ = memory_measurements(agent, plan, step, monkeypatch)
        await decisions.decision_context(agent, plan, step, context)
        for index in range(3):
            (work / "code.txt").write_text(f"change {index}\n")
            current = await decisions.decision_context(agent, plan, step, context)
            assert current.evidence.passed is (index < 2)
        assert current.evidence.stale_reruns_used == 2 and current.evidence.rechecks_used == 0
        assert current.evidence.stale == ("c1",)
        assert protocol.Decision.ACCEPT not in current.allowed()
        assert protocol.Decision.REVIEW not in current.allowed()
        assert protocol.Decision.RECHECK not in current.allowed()
        assert len([call for call in runner.run_step_checks.call_args_list if call.kwargs.get("rerun_after")]) == 2
        assert [row["state"] for row in history] == ["passed", "stale", "passed", "stale", "passed", "stale"]
    asyncio.run(run())


def test_accept_after_version_change_requires_a_new_decision_before_merging(tmp_path, monkeypatch):
    async def run():
        agent, plan, step, context, work = setup(tmp_path, monkeypatch)
        memory_measurements(agent, plan, step, monkeypatch)
        measured = await decisions.decision_context(agent, plan, step, context)
        (work / "code.txt").write_text("changed after planner turn\n")
        monkeypatch.setattr(agent, "_decide_again", AsyncMock())
        monkeypatch.setattr(agent, "_merged", AsyncMock(return_value=True))
        monkeypatch.setattr(planner.decisions, "step_decision", AsyncMock())
        await agent._apply_decision(plan, step, measured, protocol.DecisionReply(protocol.Decision.ACCEPT, "", "", (), ""))
        agent._decide_again.assert_awaited_once_with(plan, step)
        agent._merged.assert_not_awaited()
        planner.decisions.step_decision.assert_not_awaited()
        assert step.status == "deciding"
    asyncio.run(run())


@pytest.mark.parametrize("finished_before_restart", [False, True])
def test_pending_recheck_resumes_without_spending_another_recheck_or_retry(tmp_path, monkeypatch, finished_before_restart):
    async def run():
        agent, plan, step, context, _ = setup(tmp_path, monkeypatch)
        history, append = memory_measurements(agent, plan, step, monkeypatch)
        append("error")["error_kind"] = "interrupted"
        decisions.EvidenceState(rechecks_used=1, blocked=True, pending=decisions.Rerun.RECHECK,
                                after={"c1": 1}, attempt=1).store(step)
        if finished_before_restart:
            append("passed")
        persisted = deepcopy(step.result)
        # The newly constructed Planner only has the saved step result and evidence history.
        restarted = planner.PlannerAgent(task_id=agent._task_id, runtime_root=agent._runtime_root)
        restarted._execution = agent._execution
        monkeypatch.setattr(restarted, "_save", AsyncMock())
        monkeypatch.setattr(restarted, "_activity", AsyncMock())
        step.result = persisted
        current = await decisions.decision_context(restarted, plan, step, context)
        assert current.evidence.passed and len(history) == 2
        assert current.evidence.rechecks_used == 1 and current.evidence.stale_reruns_used == 0
        assert decisions.EvidenceState.read(step).pending is None
        assert step.retries == 0 and history[-1]["run"] == 2
        assert runner.run_step_checks.call_args.kwargs["rerun_after"] == {"c1": 1}
    asyncio.run(run())


def test_real_runner_and_database_invalidate_then_rerun_before_reviewer(tmp_path, monkeypatch):
    from looper.db import plans, projects, tasks

    async def run(pool):
        agent, plan, step, context, work = setup(tmp_path, monkeypatch)
        owner = await projects.create_project(name="Evidence versions", pool=pool)
        await projects.set_acceptance_checks(owner["id"], "network", pool=pool)
        await tasks.create_task(task_id=agent._task_id, requirement="Verify", goal_id="goal", project_id=owner["id"],
                                work_dir=str(work), status="running", pool=pool)
        await plans.save(agent._task_id, plan={"plan_id": plan.plan_id, "status": "executing"},
                         steps=[planner._step_row(step)], pool=pool)
        context = await decisions.decision_context(agent, plan, step, context)
        assert context.evidence.passed
        original = (await step_check_runs.list_runs(agent._task_id, step.step_id))[0]
        (work / "code.txt").write_text("new implementation\n")
        monkeypatch.setattr(agent, "_decide_again", AsyncMock())
        payload = await decisions.reviewer_payload(agent, plan, step)
        rows = await step_check_runs.list_runs(agent._task_id, step.step_id)
        assert [row["state"] for row in rows] == ["passed", "stale", "passed"]
        assert rows[0] == original and rows[2]["content_sha"] != rows[0]["content_sha"]
        assert all(row["attempt"] == 1 for row in rows)
        assert payload is None  # A new version requires a fresh decision and review round.
        agent._decide_again.assert_awaited_once_with(plan, step)
        payload = await decisions.reviewer_payload(agent, plan, step)
        assert "Acceptance evidence" in payload["evidence"] and "PASSED" in payload["evidence"]
        assert current_evidence(rows) == (rows[2],)
    run_db(run, monkeypatch)


@pytest.mark.parametrize("continue_plan", [False, True])
def test_interrupted_unfrozen_merge_cannot_adopt_new_unmeasured_source(tmp_path, monkeypatch, continue_plan):
    from types import SimpleNamespace
    from looper.agents.step_workspaces import StepWorkspaceState
    async def run():
        agent, plan, step, context, work = setup(tmp_path, monkeypatch)
        memory_measurements(agent, plan, step, monkeypatch)
        await decisions.decision_context(agent, plan, step, context)
        step.workspace = SimpleNamespace(state=StepWorkspaceState.MERGING, result_commit=None, work_dir=str(work))
        (work / "code.txt").write_text("changed while stopped before commit_pending\n")
        monkeypatch.setattr(agent, "_decide_again", AsyncMock())
        monkeypatch.setattr(planner.workspaces, "merge", AsyncMock())
        monkeypatch.setattr(agent, "_step_settled", AsyncMock())
        if continue_plan:
            assert not await agent._merged(plan, step, decide=False)
            agent._decide_again.assert_not_awaited()
        else:
            await agent._finish_merge(plan, step)
            agent._decide_again.assert_awaited_once_with(plan, step)
        planner.workspaces.merge.assert_not_awaited()
        agent._step_settled.assert_not_awaited()
        assert step.status == "deciding"
        assert decisions.EvidenceState.read(step).stale_reruns_used == 1
    asyncio.run(run())
