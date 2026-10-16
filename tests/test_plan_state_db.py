"""A task's plan and the agents' Claude sessions in a real PostgreSQL.

The Planner saves its plan row by row and a Planner started later reads it
back; the Manager's and the Planner's sessions live in ``agent_sessions``;
the server resumes the tasks it left running with a live plan; and the plan
and session files kept before are imported once. Runs only when
LOOPER_TEST_DATABASE_URL points at a disposable database.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from looper.agents.run_usage import Usage
from looper.agents.tiers.planner import PlannerAgent, PlanStep, TaskPlan
from tests.test_usage_api_db import DSN, _run

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")


async def _task(pool: Any, task_id: str = "task-a", *, status: str = "running") -> str:
    from looper.db import projects, tasks

    project = await projects.create_project(name=f"calc-{task_id}", work_dir="/w", pool=pool)
    await tasks.create_task(task_id=task_id, requirement="build calc", goal_id="g1", project_id=project["id"],
                            work_dir="/w", correlation_id="corr-1", status=status, pool=pool)
    return project["id"]


def _plan() -> TaskPlan:
    plan = TaskPlan(plan_id="plan-1", goal_id="g1", requirement="build calc", work_dir="/w", status="executing",
                    correlation_id="corr-1", origin_channel_id="channel-web", session_id="sess-p",
                    session_totals=Usage(cost_usd=0.75, input_tokens=12))
    plan.steps = [
        PlanStep(step_id="step-1", index=0, instruction="do s1", key="s1", status="verified",
                 acceptance_criteria=["s1 works"], worker_id="worker-1", result={"status": "completed",
                                                                                "summary": "did s1"}),
        PlanStep(step_id="step-2", index=1, instruction="do s2", key="s2", status="dispatched", review=True,
                 depends_on=["step-1"], worker_id="worker-3", retries=1, feedback=["fix the parser"],
                 missed_messages=["also add tests"], target_files=["calc.py"]),
    ]
    return plan


def test_a_restarted_planner_reads_back_its_plan_and_session(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Every field the Planner decides with comes back, from rows written one change at a time."""
    from looper.db import tasks

    async def test(pool: Any) -> None:
        project_id = await _task(pool)
        first = PlannerAgent(task_id="task-a", runtime_root=tmp_path)
        saved = _plan()
        await first._save(saved, *saved.steps, plan_row=True)
        await first._save_session(saved)
        # A later change writes only its step's row.
        saved.steps[0].reviewer_id = "reviewer-9"
        await first._save(saved, saved.steps[0])

        second = PlannerAgent(task_id="task-a", runtime_root=tmp_path)
        await second._load_plan()
        loaded = second._plan
        assert loaded is not None
        assert (loaded.plan_id, loaded.status, loaded.requirement, loaded.work_dir, loaded.goal_id) == (
            "plan-1", "executing", "build calc", "/w", "g1")
        assert (loaded.correlation_id, loaded.origin_channel_id, loaded.project_id) == (
            "corr-1", "channel-web", project_id)
        assert (loaded.session_id, loaded.session_totals) == ("sess-p", Usage(cost_usd=0.75, input_tokens=12))
        assert loaded.steps == saved.steps

        # A plan that ended is not read back as live, and the dashboard reads the same rows.
        saved.status = "completed"
        await first._save(saved, plan_row=True)
        third = PlannerAgent(task_id="task-a", runtime_root=tmp_path)
        await third._load_plan()
        assert third._plan is None
        detail = await tasks.get_task_with_steps("task-a", pool=pool)
        assert [(s["step_key"], s["status"], s["retries"]) for s in detail["steps"]] == [
            ("s1", "verified", 0), ("s2", "dispatched", 1)]
        assert not (tmp_path / ".local").exists()  # no plan file

    _run(test, monkeypatch)


def test_resumable_tasks_and_restart_cleanup(monkeypatch: pytest.MonkeyPatch) -> None:
    """A running task with a live plan is resumed, any other running one stopped; a new start drops the plan."""
    from looper.db import agent_sessions, plans, tasks

    async def test(pool: Any) -> None:
        for task_id, status, plan_status in (("task-live", "running", "executing"),
                                             ("task-review", "running", "verifying"),
                                             ("task-verifying", "verifying", "executing"),
                                             ("task-ended", "running", "failed"),
                                             ("task-stopped", "stopped", "executing"),
                                             ("task-unplanned", "running", None),
                                             ("task-planning", "planning", "planning")):
            await _task(pool, task_id, status=status)
            if plan_status:
                await plans.save(task_id, plan={"plan_id": f"plan-{task_id}", "status": plan_status},
                                 steps=[{"id": f"step-{task_id}", "index": 0, "instruction": "x"}], pool=pool)
                await agent_sessions.save_session(f"planner-{task_id}", "sess", {"cost_usd": 0.1}, pool=pool)
        resumable = await plans.resumable_task_ids(tasks.ACTIVE_STATUSES, pool=pool)
        # A task the Planner left in one of its own statuses counts as running too.
        assert resumable == ["task-live", "task-review", "task-verifying"]
        assert await tasks.mark_orphaned_tasks_stopped(keep=resumable, pool=pool) == 3
        statuses = {t["id"]: t["status"] for t in await tasks.list_tasks(pool=pool)}
        assert statuses == {"task-live": "running", "task-review": "running", "task-verifying": "verifying",
                            "task-ended": "stopped", "task-stopped": "stopped", "task-unplanned": "stopped",
                            "task-planning": "stopped"}

        # Continuing keeps the plan and the session; starting anew and deleting drop them.
        await tasks.reset_task_for_restart("task-live", keep_steps=True, pool=pool)
        assert await plans.load("task-live", pool=pool) is not None
        assert await agent_sessions.get_session("planner-task-live", pool=pool) is not None
        await tasks.reset_task_for_restart("task-review", pool=pool)
        await tasks.delete_task("task-ended", pool=pool)
        for task_id in ("task-review", "task-ended"):
            assert await plans.load(task_id, pool=pool) is None
            assert await agent_sessions.get_session(f"planner-{task_id}", pool=pool) is None

    _run(test, monkeypatch)


# --- The one-time import of the state files ---

CURRENT_PLAN = {
    "plan_id": "plan-new", "goal_id": "g1", "requirement": "build calc", "status": "executing",
    "project_id": "proj-x", "project_name": "calc", "project_description": "", "project_background": "",
    "project_planner_harness": "", "project_updated_at": "", "project_work_dir": "/w",
    "session_id": "sess-file", "session_totals": {"cost_usd": 0.42, "input_tokens": 10},
    "final_summary": "", "fail_reason": "",
    "steps": [
        {"step_id": "step-1", "index": 0, "key": "s1", "instruction": "do s1", "status": "verified",
         "acceptance_criteria": ["s1 works"], "review": False, "target_files": [], "depends_on": [],
         "worker_id": "worker-1", "reviewer_id": None, "retries": 0, "feedback": []},
        {"step_id": "step-2", "index": 1, "key": "s2", "instruction": "do s2", "status": "dispatched",
         "acceptance_criteria": [], "review": True, "target_files": ["calc.py"], "depends_on": ["step-1"],
         "worker_id": "worker-2", "reviewer_id": None, "retries": 1, "feedback": ["fix it"]},
    ],
    "correlation_id": "corr-1", "origin_channel_id": "channel-web", "work_dir": "/w",
}
# A plan file from before the resident Planner.
OLD_PLAN = {
    "plan_id": "plan-old", "goal_id": "", "requirement": "x", "status": "completed", "project_root": "/w",
    "correlation_id": "", "origin_channel_id": "",
    "steps": [{"step_id": "s", "index": 0, "instruction": "x", "status": "verified", "complexity": "low",
               "target_files": [], "depends_on": []}],
}


def _write(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")


def test_the_state_files_are_imported_once(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from looper.agents.state_import import import_file_state
    from looper.db import agent_sessions, plans

    planner_dir = tmp_path / ".local" / "planner"
    _write(planner_dir / "planner-task-new" / "plan-new.json", CURRENT_PLAN)
    _write(planner_dir / "planner-task-old" / "plan-old.json", OLD_PLAN)
    _write(planner_dir / "planner-task-gone" / "plan-gone.json", {**CURRENT_PLAN, "plan_id": "plan-gone"})
    _write(tmp_path / ".local" / "manager" / "session.json",
           {"session_id": "sess-manager", "session_totals": {"cost_usd": 1.5}})

    async def test(pool: Any) -> None:
        await _task(pool, "task-new")
        await _task(pool, "task-old")
        # The step rows the Planner wrote before; the file never held their results.
        await plans.save("task-new", steps=[{"id": "step-1", "index": 0, "instruction": "do s1", "status": "verified",
                                             "result": {"status": "completed", "summary": "did s1"}}], pool=pool)

        report = await import_file_state(tmp_path, pool=pool)
        assert report.to_dict() == {
            "done_before": False, "plans": 1, "steps": 2, "sessions": 2, "skipped_old_format": 1,
            "skipped_no_task": 1, "skipped_in_database": 0, "skipped_superseded": 0, "skipped_unreadable": 0}
        stored = await plans.load("task-new", pool=pool)
        assert stored is not None
        assert {k: stored["plan"][k] for k in ("plan_id", "status", "correlation_id", "origin_channel_id")} == {
            "plan_id": "plan-new", "status": "executing", "correlation_id": "corr-1",
            "origin_channel_id": "channel-web"}
        one, two = stored["steps"]
        assert (one["step_key"], one["result"]) == ("s1", {"status": "completed", "summary": "did s1"})
        assert (two["step_key"], two["depends_on"], two["review"], two["retries"], two["feedback"]) == (
            "s2", ["step-1"], True, 1, ["fix it"])
        # Sessions from files predate agents: Claude Code ran them (no runtime type recorded).
        before_agents = {"runtime_type": None, "provider_id": None, "profile_id": None, "clean_start": False}
        assert await agent_sessions.get_session("planner-task-new", pool=pool) == {
            "session_id": "sess-file", "session_totals": {"cost_usd": 0.42, "input_tokens": 10}, **before_agents}
        assert await agent_sessions.get_session("manager", pool=pool) == {
            "session_id": "sess-manager", "session_totals": {"cost_usd": 1.5}, **before_agents}
        assert await plans.load("task-old", pool=pool) is None

        # Recorded: a second start does nothing, even with a changed file; the files stay.
        _write(planner_dir / "planner-task-new" / "plan-new.json", {**CURRENT_PLAN, "status": "failed"})
        assert (await import_file_state(tmp_path, pool=pool)).to_dict() == {
            "done_before": True, "plans": 0, "steps": 0, "sessions": 0, "skipped_old_format": 0,
            "skipped_no_task": 0, "skipped_in_database": 0, "skipped_superseded": 0, "skipped_unreadable": 0}
        assert (await plans.load("task-new", pool=pool))["plan"]["status"] == "executing"  # type: ignore[index]
        assert len(list(planner_dir.glob("*/*.json"))) == 3

    _run(test, monkeypatch)


def test_a_manager_session_dropped_with_new_stays_clean_after_a_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """/new drops the Manager's session with ``clean_start``, so a Manager started later begins without a resume
    brief; the reset is a run under the session it ended."""
    from looper.agents.bus import MessageBus
    from looper.agents.tiers.manager import ManagerAgent, ManagerConfig
    from looper.db import agent_sessions, llm_records
    from tests.test_manager_new_session import _web

    async def test(pool: Any) -> None:
        await agent_sessions.save_session("manager", "sess-old", {"cost_usd": 1.0}, runtime_type="claude-code",
                                          pool=pool)
        first = ManagerAgent(config=ManagerConfig(work_dir=tmp_path), runtime_root=tmp_path)
        first._bus = MessageBus(runtime_root=tmp_path)
        await first._handle(_web("/new"))
        saved = await agent_sessions.get_session("manager", pool=pool)
        assert saved is not None and (saved["session_id"], saved["clean_start"]) == (None, True)
        later = ManagerAgent(config=ManagerConfig(work_dir=tmp_path), runtime_root=tmp_path)  # after a restart
        await later._load_session_state()
        assert (later._session_id, later._clean_start) == (None, True)
        (reset,) = await llm_records.list_records(agent_id="manager", pool=pool)
        assert (reset["session_id"], reset["status"], reset["cost_usd"]) == ("sess-old", "completed", 0.0)
        assert reset["metadata"]["session_reset"] == {"channel": "web", "sender": None, "chat": "session-a",
                                                      "group": False, "previous_session_id": "sess-old"}

    _run(test, monkeypatch)
