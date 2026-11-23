"""Acceptance collection runs after successful work, without changing Planner decisions."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from looper.agents import system_prompt
from looper.agents.project_execution import AcceptanceChecksMode, ProjectExecution
from looper.agents.tiers import planner, planner_protocol, worker
from looper.db import plans, projects, tasks
from test_acceptance_checks_off import _main_modules, _outputs, _step
from test_planner_agent import WORK_FAILED, WORK_OK, decide, drive, plan, step
from test_planner_workspaces import git, init_repo, project
from tests.db_support import DSN, run_db


@pytest.mark.parametrize("mode,succeeded,has_checks,expected", [
    ("local", True, True, True), ("network", True, True, True),
    ("local", False, True, False), ("local", True, False, False),
    ("off", True, False, False),
])
def test_worker_result_collects_before_decision_without_changing_allowed_actions(
    planner_claude, agent_db, monkeypatch, mode, succeeded, has_checks, expected,
):
    from looper.agents import acceptance_runner

    agent_db.get_task_project.return_value = project(acceptance_checks=mode,
        workspace_links=[".venv"], verification_paths=["node_modules"])
    item = step("s1")
    if has_checks:
        item["checks"] = [{"id": "c1", "criterion": 1, "type": "command", "command": "exit 1"}]
    claude = planner_claude(plan(item), decide("accept" if succeeded else "fail"))
    observed = []

    async def collect(**kwargs):
        saved = next(iter(agent_db.store.steps["t1"].values()))
        assert saved["status"] == "deciding" and saved["result"]["status"] == "completed"
        assert len(claude.turns()) == 1  # no DECISION turn before the evidence write
        observed.append(kwargs)
        return [{"state": "failed", "check_id": "c1"}]

    monkeypatch.setattr(acceptance_runner, "run_step_checks", collect)
    result = WORK_OK if succeeded else WORK_FAILED
    h = drive(claude, lambda _: {**result, "acceptance_baseline": {"head_sha": "forged"}})
    assert bool(observed) is expected
    assert h.final().payload["status"] == ("completed" if succeeded else "failed")
    if expected:
        call = observed[0]
        assert call["task_id"] == "t1" and call["attempt"] == 1
        assert call["work_dir"] == h.work_dir
        assert call["mode"] is AcceptanceChecksMode(mode)
        assert tuple(call["runtime_paths"]) == (".venv", "node_modules")
        assert call["base_commit"] is None  # report-only directory; the Worker cannot forge a baseline
        decision = claude.turns()[-1]["text"]
        assert "Allowed decisions now: accept" in decision
        assert "Acceptance evidence" not in decision and "recheck" not in decision


def test_dispatch_captures_protected_baseline_and_keeps_it_across_same_attempt_restart(
    planner_claude, agent_db, monkeypatch,
):
    from looper.agents import acceptance_runner

    agent_db.get_task_project.return_value = project(acceptance_checks="local")
    item = {**step("s1"), "checks": [{"id": "c1", "criterion": 1, "type": "command",
                                      "command": "exit 0", "protect": ["shared.txt"]}]}
    claude = planner_claude(plan(item), decide("accept"))
    observed = []

    async def collect(**kwargs):
        observed.append(kwargs)
        return []

    monkeypatch.setattr(acceptance_runner, "run_step_checks", collect)

    async def scenario(h):
        head = init_repo(h.work_dir)
        await h.start()
        await h.until(lambda: bool(h.held))
        running = h.planner._plan.steps[0]
        assert running.result["acceptance_baseline"] == {"attempt": 1, "head_sha": head}
        (h.work_dir / "shared.txt").write_text("changed by Worker\n")
        git(h.work_dir, "add", "shared.txt")
        git(h.work_dir, "commit", "-qm", "Worker change")
        await h.planner._dispatch_step(h.planner._plan, running, same_attempt=True)
        assert running.result["acceptance_baseline"]["head_sha"] == head
        # Answer only the latest dispatch; the old Worker's delayed result is ignored.
        await h.answer(h.held[-1], {**WORK_OK, "acceptance_baseline": {"head_sha": "forged"}})
        await h.finished()
        assert observed[0]["base_commit"] == head

    drive(claude, lambda _: None, scenario)


def test_resumed_success_collects_again_before_asking_the_saved_decision(tmp_path, monkeypatch):
    from looper.agents import acceptance_runner
    from looper.agents.acceptance_checks import CommandCheck

    async def run():
        agent = planner.PlannerAgent(task_id="task-resume", runtime_root=tmp_path)
        agent._execution = ProjectExecution(acceptance_checks=AcceptanceChecksMode.LOCAL)
        current = planner.PlanStep("step-resume", 0, "Report", key="s1", status="deciding",
            acceptance_criteria=["Report works"], checks=(CommandCheck("c1", 1, "exit 0"),),
            result={"status": "completed", "acceptance_baseline": {"attempt": 1, "head_sha": "abc"}})
        task = planner.TaskPlan("plan", "goal", "Report", str(tmp_path), [current], status="executing")
        agent._plan = task
        events = []
        monkeypatch.setattr(agent, "_save", AsyncMock(side_effect=lambda *a, **k: events.append("saved")))
        monkeypatch.setattr(agent, "_room_lines", AsyncMock(return_value=[]))
        monkeypatch.setattr(agent, "_asking", AsyncMock(return_value=lambda x: x))
        monkeypatch.setattr(agent, "_ask", AsyncMock(side_effect=lambda *a, **k:
            events.append("decision") or SimpleNamespace(decision=planner_protocol.Decision.ACCEPT)))
        monkeypatch.setattr(agent, "_apply_decision", AsyncMock())
        monkeypatch.setattr(acceptance_runner, "run_step_checks", AsyncMock(side_effect=lambda **k:
            events.append(("checks", k["base_commit"]))))
        await agent._decide_again(task, current)
        assert events == ["saved", ("checks", "abc"), "decision"]

    asyncio.run(run())


@pytest.mark.parametrize("current_mode", ["off", "local"])
def test_saved_network_checks_can_be_loaded_after_permission_changes(tmp_path, monkeypatch, current_mode):
    async def run():
        agent = planner.PlannerAgent(task_id="task-saved", runtime_root=tmp_path)
        stored_step = planner._step_row(planner.PlanStep("step-saved", 0, "Report", status="deciding",
            acceptance_criteria=["Report works"], result={"status": "completed"}))
        stored_step["checks"] = [{"id": "c1", "criterion": 1, "type": "command", "command": "exit 0",
                                  "network": True}]
        monkeypatch.setattr(planner.db_plans, "load", AsyncMock(return_value={
            "plan": {"plan_id": "plan-saved", "status": "executing"}, "steps": [stored_step]}))
        monkeypatch.setattr(planner.db_agent_sessions, "get_session", AsyncMock(return_value=None))
        monkeypatch.setattr(planner.db_projects, "get_task_project", AsyncMock(return_value={
            "acceptance_checks": current_mode}))
        monkeypatch.setattr(planner.db_step_workspaces, "list_for_task", AsyncMock(return_value=[]))
        await agent._load_plan()
        assert agent._plan is not None
        saved = agent._plan.steps[0]
        assert saved.checks[0].network is True
        baseline = _main_modules("origin/main")
        old = baseline.planner.PlannerAgent(task_id="task-saved", runtime_root=tmp_path)
        context = planner_protocol.DecisionContext("s1", planner_protocol.ResultKind.WORK, True, False, 0)
        messages = [agent._decision_message(agent._plan, saved, context, ["Worker finished."]),
                    agent._supervise_message(agent._plan, [saved], [["Worker running."]]),
                    "[CHAT] Continue.\n" + planner.PLAN_VIEW]
        for mode in (current_mode, "off"):
            monkeypatch.setattr(planner.db_projects, "get_task_project", AsyncMock(return_value={
                "acceptance_checks": mode}))
            await agent._refresh_project(agent._plan)
            for message in messages:
                rendered, _ = agent._render_plan_message(agent._plan, message, full_reason="new process")
                if mode == "off":
                    original, _ = old._render_plan_message(agent._plan, message, full_reason="new process")
                    assert rendered.encode() == original.encode()
                else:
                    assert '"command": "exit 0"' in rendered  # positive control for enabled rendering
            assert saved.checks[0].network is True  # disabling changes visibility, not stored definitions

    asyncio.run(run())


@pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")
@pytest.mark.parametrize("status", ["pending", "dispatched", "deciding", "completed", "verified"])
def test_switch_off_stored_rows_and_prompts_match_current_main_bytes(tmp_path, monkeypatch, status):
    # Compare with the fetched main source, rather than another configuration of this implementation.
    baseline = _main_modules("origin/main")
    current = SimpleNamespace(planner=planner, protocol=planner_protocol, prompt=system_prompt,
                              tasks=tasks, worker=worker)
    assert _outputs(current, tmp_path, ProjectExecution.from_row({"acceptance_checks": "off"})) == (
        _outputs(baseline, tmp_path))

    async def check(pool):
        owner = await projects.create_project(name="Default-off compatibility", pool=pool)
        await tasks.create_task(task_id="task-off", project_id=owner["id"], requirement="Report", pool=pool)
        async with pool.acquire() as conn, conn.transaction():
            before_step = baseline.planner._step_row(_step(baseline.planner, status))
            await conn.execute(baseline.plans._SAVE_STEP, *baseline.plans._step_args("task-off", before_step))
            before = await conn.fetchval("SELECT (to_jsonb(s) - 'checks')::text FROM task_steps s WHERE id='step-off'")
            await conn.execute("DELETE FROM task_steps WHERE id='step-off'")
            after_step = planner._step_row(_step(planner, status))
            await conn.execute(plans._SAVE_STEP, *plans._step_args("task-off", after_step))
            after = await conn.fetchval("SELECT (to_jsonb(s) - 'checks')::text FROM task_steps s WHERE id='step-off'")
            assert before.encode() == after.encode()
            assert await conn.fetchval("SELECT count(*) FROM step_check_runs") == 0
    run_db(check, monkeypatch)
