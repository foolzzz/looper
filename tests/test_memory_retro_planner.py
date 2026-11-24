"""Project retrospectives replace legacy project knowledge without owning vault closeout."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from looper.agents import memory_lessons
from looper.agents.project_execution import ProjectExecution
from looper.agents.tiers import planner
from looper.agents.tiers.journal import JournalEntry, JournalKind
from tests.db_support import run_db
from tests.memory_lessons_support import lesson


@pytest.mark.parametrize("enabled,project_mode,problem,replaced", [
    (True, "on", None, True),
    (False, "on", None, False),
    (True, "off", None, False),
    (True, None, None, False),
    (True, "on", "invalid stored settings", False),
    (True, "on", RuntimeError("settings unavailable"), False),
])
@pytest.mark.parametrize("ending", ["completed", "failed"])
def test_project_lessons_switch_keeps_journal_and_vault_closeout(
    monkeypatch, tmp_path, enabled, project_mode, problem, replaced, ending,
):
    async def check():
        monkeypatch.setattr(memory_lessons, "settings_view", AsyncMock(return_value={
            "enabled": enabled, "problem": problem, "settings": {"auto_activate": False}}))
        if isinstance(problem, Exception):
            memory_lessons.settings_view.side_effect = problem
        monkeypatch.setattr(planner, "update_task_phase", AsyncMock())
        monkeypatch.setattr(planner.acceptance_result, "for_plan", AsyncMock(return_value=None))
        add_draft = AsyncMock()
        monkeypatch.setattr(planner.project_knowledge, "add_draft", add_draft)
        agent = planner.PlannerAgent(task_id="task-synthetic", runtime_root=tmp_path)
        agent._execution = ProjectExecution.from_row({"distill_lessons": "draft", "memory_lessons": project_mode})
        agent._bus = SimpleNamespace(publish=AsyncMock())
        for name in ("_save", "_activity", "_remove_workspaces", "_distill_lessons", "_vault_closeout"):
            setattr(agent, name, AsyncMock())
        agent._process_record = AsyncMock(return_value=None)
        step = planner.PlanStep("step-synthetic", 0, "Check the parser", key="s1", status="verified")
        entry = JournalEntry(step_id=step.step_id, step=step.label, kind=JournalKind.STEP_ACCEPTED,
                             summary="The parser was checked", learnings=[])
        plan = planner.TaskPlan("plan-synthetic", "goal-synthetic", "Check the parser", "", steps=[step],
                                project_id="project-synthetic", journal=[entry], final_summary="Checked")
        await agent._capture_project_learnings(plan, step, [{"text": "Run the parser check", "scope": "project"}],
                                               "Worker")
        assert add_draft.await_count == (0 if replaced else 1)
        if ending == "completed":
            await agent._complete_plan(plan)
            assert agent._distill_lessons.await_count == (0 if replaced else 1)
        else:
            await agent._fail_plan(plan, reason="Synthetic failure")
            agent._distill_lessons.assert_not_awaited()
        assert plan.journal == [entry]
        agent._vault_closeout.assert_awaited_once()
        assert agent._bus.publish.await_args.args[0].payload["status"] == ending
    asyncio.run(check())


def test_delete_confirmation_counts_sourced_active_lessons_once(monkeypatch):
    from looper.agents.v1_domains import workspaces

    async def check(pool):
        label = await lesson(pool, "project-synthetic", "First lesson")
        number = int(label.removeprefix("M-"))
        task_id = "task-First lesson"
        operation = next(op for op in workspaces.OPERATIONS if op.operation_id == "tasks.delete")
        arguments = {"task_id": task_id}
        original = (f"Delete task {task_id} (completed), its step worktrees, task checkout, "
                    "saved sessions and database records. The task's result branch stays.")
        assert await workspaces.impact(None, operation, arguments, None, {}) == original
        await pool.execute("UPDATE memory_candidates SET state='in_effect',reason=NULL WHERE id=$1", number)
        extra = await pool.fetchval("INSERT INTO task_journal(task_id,step_id,kind,summary) "
                                   "VALUES ($1,'s2','step_accepted','More evidence') RETURNING id", task_id)
        await pool.execute("INSERT INTO memory_candidate_sources(candidate_id,kind,journal_id) "
                           "VALUES ($1,'journal',$2)", number, extra)
        text = await workspaces.impact(None, operation, arguments, None, {})
        assert text == original + (" This also deletes 1 in-effect project lesson(s) sourced from this task's "
                                   "journal. Promote lessons you want to keep to the vault first.")
        await pool.execute("INSERT INTO tasks(id,requirement,project_id,status) "
                           "VALUES ('task-other','Other task','project-synthetic','completed')")
        other = await workspaces.impact(None, operation, {"task_id": "task-other"}, None, {})
        assert "lesson(s)" not in other
        await pool.execute("DELETE FROM tasks WHERE id=$1", task_id)
        assert await pool.fetchval("SELECT count(*) FROM memory_candidates WHERE id=$1", number) == 0
        assert await pool.fetchval("SELECT count(*) FROM memory_tombstones") == 0
    run_db(check, monkeypatch, timeout=30)


@pytest.mark.parametrize("enabled,project_mode,marked", [(True, "on", True), (False, "on", False),
                                                          (True, "off", False)])
def test_project_scope_survives_the_real_journal_write(monkeypatch, tmp_path, enabled, project_mode, marked):
    async def check():
        monkeypatch.setattr(memory_lessons, "settings_view", AsyncMock(return_value={
            "enabled": enabled, "problem": None}))
        save = AsyncMock()
        monkeypatch.setattr(planner.db_plans, "save", save)
        agent = planner.PlannerAgent(task_id="task-synthetic", runtime_root=tmp_path)
        agent._execution = ProjectExecution.from_row({"memory_lessons": project_mode})
        step = planner.PlanStep("step-synthetic", 0, "Check the parser", status="verified", result={
            "summary": "Checked", "learnings": ["Only this task", {"text": "Check the parser", "scope": "project"}],
            "verify_learnings": [{"text": "Review the parser", "scope": "project"}],
        })
        plan = planner.TaskPlan("plan-synthetic", "goal-synthetic", "Check", "", steps=[step])
        entry = agent._accepted_entry(plan, step)
        await agent._save(plan, step, entries=[entry])
        expected = ["Only this task", "Check the parser", "Review the parser"]
        if marked:
            expected[1:] = ["(holds for the whole project) " + text for text in expected[1:]]
        assert save.await_args.kwargs["journal"][0]["learnings"] == expected
        assert list(plan.journal[0].learnings) == expected
    asyncio.run(check())
