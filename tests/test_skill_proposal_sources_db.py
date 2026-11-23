"""Proposal sources and durable, once-only feedback on a disposable PostgreSQL schema."""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from looper.agents import api_v1, skill_proposals as proposals
from looper.agents import skill_proposal_sources as sources
from looper.db import projects
from tests.db_support import DSN, run_db

pytestmark = pytest.mark.skipif(not DSN, reason="requires disposable LOOPER_TEST_DATABASE_URL")


def item(name="release-check", **changes):
    return {"action": "create", "name": name, "description": "Use before a project release.",
            "content": "Run the project checks before publishing.\n", "why": "Repeated during two steps.", **changes}


async def project_task(pool, mode="on"):
    project = await projects.create_project(name="Proposal source", pool=pool)
    await proposals.set_mode(project["id"], mode)
    await pool.execute("INSERT INTO tasks (id, project_id, requirement, status) VALUES ('task-one', $1, 'r', 'executing')",
                       project["id"])
    await pool.execute("INSERT INTO task_steps (id, task_id, step_index, step_key, instruction) "
                       "VALUES ('step-one', 'task-one', 0, 's2', 'repeat release checks')")
    return project["id"]


def test_closing_reply_stores_two_pending_drafts_and_activity(monkeypatch, tmp_path):
    from looper.agents.tiers.planner import PlannerAgent, PlanStep, TaskPlan
    from looper.agents.tiers.planner_protocol import DecisionContext, PlanView, ResultKind, parse_decision

    async def test(pool):
        project = await project_task(pool)
        planner = PlannerAgent(task_id="task-one", runtime_root=tmp_path)
        step = PlanStep("step-one", 0, "Repeat checks", key="s2", status="completed")
        plan = TaskPlan("plan", "goal", "Check releases", str(tmp_path), steps=[step], project_id=project)
        planner._plan = plan
        # Git merge and the subsequent dispatch are outside this decision's persistence contract.
        monkeypatch.setattr(planner, "_merged", AsyncMock(return_value=True))
        monkeypatch.setattr(planner, "_step_settled", AsyncMock())
        context = DecisionContext("s2", ResultKind.WORK, True, False, 0, last_open=True)
        reply = parse_decision("```json\n" + json.dumps({"decision": "accept", "skill_proposals": [item(), item("test-check")],
            "final": {key: "done" for key in ("delivered", "how_to_use", "verification", "open_issues")}}) + "\n```",
            context, PlanView({"s2": ()}, {}), skill_proposals=True)
        await planner._decided(plan, step, context, reply)
        assert step.status == "verified" and plan.final_summary == reply.final.markdown()
        rows = await pool.fetch("SELECT id, status, evidence, proposer FROM skill_proposals ORDER BY id")
        assert len(rows) == 2 and all(row["status"] == "pending" for row in rows)
        assert all(row["proposer"] == "planner-task-one" for row in rows)
        events = await pool.fetch("SELECT via FROM skill_proposal_events ORDER BY id")
        assert [row["via"] for row in events] == ["planner", "planner"]
        lines = await pool.fetch("SELECT summary FROM activity WHERE task_id='task-one' AND summary LIKE 'Skill proposal%'")
        assert len(lines) == 2
        assert all("from step s2 created" in row["summary"] for row in lines)
        assert await pool.fetchval("SELECT count(*) FROM library_items") == 0
    run_db(test, monkeypatch)


def test_secret_refusal_is_safely_reported_to_next_planner_and_task_completes(monkeypatch):
    async def test(pool):
        project = await project_task(pool)
        secret = "ghp_" + "a1B2" * 9
        plan = SimpleNamespace(project_id=project)
        reply = SimpleNamespace(final=SimpleNamespace(markdown=lambda: "Completed."),
                                skill_proposals=(item(content=f"Use {secret}."),))
        planner = SimpleNamespace(_task_id="task-one", _last_record_id=None)
        assert await sources.final_summary(planner, plan, reply) == "Completed."
        await pool.execute("UPDATE tasks SET status='completed' WHERE id='task-one'")
        assert await pool.fetchval("SELECT count(*) FROM skill_proposals") == 0
        notes = await sources.planner_note(project)
        assert len(notes) == 1 and "refused" in notes[0] and "secret" in notes[0]
        assert secret not in notes[0]
        assert await sources.planner_note(project) == []
        activity = await pool.fetchval("SELECT summary FROM activity WHERE task_id='task-one'")
        assert "refused" in activity and secret not in activity
    run_db(test, monkeypatch)


def test_decisions_are_project_scoped_delivered_once_and_include_rejection_reason(monkeypatch):
    async def test(pool):
        project = await project_task(pool)
        await sources.from_planner("task-one", project, [item(), item("test-check")], None)
        rows = await pool.fetch("SELECT id, content_sha256 FROM skill_proposals ORDER BY id")
        await proposals.confirm(project, rows[0]["id"], rows[0]["content_sha256"], principal=api_v1.OPERATOR)
        await proposals.reject(project, rows[1]["id"], "This is task-specific.", principal=api_v1.OPERATOR)
        other = await projects.create_project(name="Unrelated", pool=pool)
        assert await sources.planner_note(other["id"]) == []
        first, second = await asyncio.gather(sources.planner_note(project), sources.planner_note(project))
        notes = first + second
        assert len(notes) == 2
        assert any("release-check: confirmed" in note for note in notes)
        assert any("test-check: rejected — This is task-specific." in note for note in notes)
        assert await sources.planner_note(project) == []
        assert await pool.fetchval("SELECT count(*) FROM skill_proposal_events WHERE told_at IS NOT NULL") == 2
    run_db(test, monkeypatch)


def test_explicit_foreign_evidence_is_refused_and_manager_keeps_typed_result(monkeypatch):
    async def test(pool):
        project = await project_task(pool)
        result = await sources.manager_propose(project, item(evidence=[{"task": "unknown"}]), proposer="manager")
        assert result.refusal is proposals.Refusal.EVIDENCE
        assert await pool.fetchval("SELECT count(*) FROM skill_proposals") == 0
        assert "refused" in (await sources.planner_note(project))[0]
    run_db(test, monkeypatch)


def test_evidence_from_another_project_task_still_records_the_proposing_tasks_activity(monkeypatch):
    async def test(pool):
        project = await project_task(pool)
        await pool.execute("INSERT INTO tasks (id, project_id, requirement) VALUES ('task-earlier', $1, 'r')", project)
        await sources.from_planner("task-one", project, [item(evidence=[{"task": "task-earlier"}])], None)
        assert await pool.fetchval("SELECT count(*) FROM skill_proposals") == 1
        assert "created" in await pool.fetchval("SELECT summary FROM activity WHERE task_id='task-one'")
    run_db(test, monkeypatch)


def test_draft_storage_failure_does_not_fail_the_closing_task(monkeypatch):
    async def test(pool):
        project = await project_task(pool)
        async def unavailable(*args, **kwargs):
            raise RuntimeError("storage unavailable")
        monkeypatch.setattr(proposals, "propose", unavailable)
        assert await sources.from_planner("task-one", project, [item()], None) is None
    run_db(test, monkeypatch)


def test_feedback_migration_fresh_marker_and_missing_object_repair(monkeypatch):
    from looper.db import connection
    from looper.db.migrations import skill_proposal_feedback as migration

    async def test(pool):
        markers = await pool.fetch("SELECT name FROM schema_migrations")
        assert [row["name"] for row in markers] == ["post_bridge_baseline"]
        assert not await migration.pending(pool)
        await pool.execute("ALTER TABLE skill_proposal_events DROP COLUMN told_at")
        assert await migration.pending(pool)
        await connection.ensure_schema(pool)
        assert not await migration.pending(pool)
        assert await pool.fetchval("SELECT EXISTS (SELECT 1 FROM schema_migrations WHERE name=$1)", migration.name)
        await connection.ensure_schema(pool)
        async with pool.acquire() as conn:
            assert not await connection.schema_change_pending(conn)
    run_db(test, monkeypatch)
