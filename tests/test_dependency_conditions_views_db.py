"""Condition persistence and task readbacks on disposable PostgreSQL."""

from types import SimpleNamespace

import pytest

from tests.db_support import DSN, run_db

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")


def test_conditions_survive_plan_writes_and_api_readback(monkeypatch, tmp_path):
    from looper.agents import api_v1, projects
    from looper.agents.ops.clients import PIPELINE_TOOL
    from looper.agents.ops.service import _plain
    from looper.db import journal, plans, projects as db_projects, tasks

    async def test(pool):
        project = await db_projects.create_project(name="conditions", pool=pool)
        await tasks.create_task(task_id="task-a", requirement="Choose a source", project_id=project["id"],
                                status="running", pool=pool)
        condition = {"any_of": ["step-s1", {"step": "step-s2", "on": "passed"}]}
        await plans.save("task-a", plan={"plan_id": "plan-a", "status": "executing"}, steps=[
            {"id": "step-s1", "index": 0, "key": "s1", "instruction": "Source one", "status": "verified"},
            {"id": "step-s2", "index": 1, "key": "s2", "instruction": "Source two", "status": "pending"},
            {"id": "step-s3", "index": 2, "key": "s3", "instruction": "Continue", "status": "pending",
             "depends_on": ["step-s1", "step-s2"], "ready_when": condition},
            {"id": "step-s4", "index": 3, "key": "s4", "instruction": "Continue serially",
             "depends_on": ["step-s1"]}], pool=pool)
        saved = await plans.load("task-a", pool=pool)
        assert saved["steps"][2]["ready_when"] == condition
        from looper.agents.tiers.planner import PlannerAgent
        restored = PlannerAgent(task_id="task-a", runtime_root=tmp_path)
        await restored._load_plan()
        assert restored._plan.step("step-s3").ready_when == condition
        assert restored._plan.step("step-s3") in restored._plan.ready_steps()
        assert restored._plan.condition_text(restored._plan.step("step-s3")) == "any of (s1, s2 passed)"
        await journal.insert(pool, "task-a", [{"step_id": "step-s3", "kind": "condition_blocked",
                                               "summary": "A dependency cannot pass"}])
        detail = _plain(await projects.task_detail("task-a"))
        api_v1.validate_arguments(api_v1.find("POST", "/api/v1/tasks/detail").result, detail)
        assert detail["steps"][2]["ready_when"] == condition
        assert detail["steps"][2]["ready_state"] == "satisfied"
        assert detail["steps"][0]["ready_state"] is None
        assert detail["steps"][1]["ready_state"] == "satisfied"
        assert detail["steps"][3]["ready_state"] == "satisfied"
        compact = await projects.task_detail("task-a", compact=True)
        assert compact["steps"][2]["ready_when_text"] == "any of (s1, s2 passed)"
        assert compact["steps"][2]["ready_state"] == "satisfied"
        assert "ready_when_text" not in compact["steps"][0]
        pipeline = await PIPELINE_TOOL.run(SimpleNamespace(arguments={"task_id": "task-a"}))
        assert pipeline["steps"][2]["ready_when_text"] == compact["steps"][2]["ready_when_text"]
        assert pipeline["steps"][2]["ready_state"] == "satisfied"
        assert "ready_state" not in pipeline["steps"][0]
        await pool.execute("UPDATE task_steps SET status = 'skipped' WHERE id = 'step-s1'")
        await pool.execute("UPDATE task_steps SET status = 'failed' WHERE id = 'step-s2'")
        after = await projects.task_detail("task-a")
        assert after["steps"][2]["ready_state"] == "unsatisfiable"
        assert after["steps"][3]["ready_state"] == "pending"
        pipeline = await PIPELINE_TOOL.run(SimpleNamespace(arguments={"task_id": "task-a"}))
        assert pipeline["steps"][2]["ready_state"] == "unsatisfiable"
        await pool.execute("UPDATE task_steps SET status = 'pending' WHERE id = 'step-s2'")
        pipeline = await PIPELINE_TOOL.run(SimpleNamespace(arguments={"task_id": "task-a"}))
        assert pipeline["steps"][2]["ready_state"] == "pending"
        await pool.execute("UPDATE task_steps SET status = 'verified' WHERE id = 'step-s3'")
        pipeline = await PIPELINE_TOOL.run(SimpleNamespace(arguments={"task_id": "task-a"}))
        assert pipeline["steps"][2]["ready_state"] is None

    run_db(test, monkeypatch, timeout=30)


def test_stored_completed_readiness_follows_current_worktree_settings(monkeypatch, tmp_path):
    from looper.agents import projects
    from looper.agents.ops.clients import PIPELINE_TOOL
    from looper.agents.tiers.conditions import State
    from looper.agents.tiers.planner import PlannerAgent
    from looper.db import plans, projects as db_projects, tasks

    async def test(pool):
        project = await db_projects.create_project(name="conditions", execution={"dependency_conditions": "on"}, pool=pool)
        await tasks.create_task(task_id="task-a", requirement="Use upstream output", project_id=project["id"],
                                status="running", pool=pool)
        condition = {"step": "step-source", "on": "completed"}
        await plans.save("task-a", plan={"plan_id": "plan-a", "status": "executing"}, steps=[
            {"id": "step-source", "index": 0, "instruction": "Source", "status": "completed",
             "result": {"status": "completed"}},
            {"id": "step-dependent", "index": 1, "instruction": "Consume", "status": "pending",
             "depends_on": ["step-source"], "ready_when": condition},
        ], pool=pool)
        for mode, status, expected in [(None, "completed", "satisfied"),
                                       ("worktree", "completed", "pending"),
                                       ("worktree", "verified", "satisfied"),
                                       ("worktree", "failed", "unsatisfiable")]:
            await pool.execute("UPDATE projects SET step_workspaces = $1 WHERE id = $2", mode, project["id"])
            await pool.execute("UPDATE task_steps SET status = $1 WHERE id = 'step-source'", status)
            restored = PlannerAgent(task_id="task-a", runtime_root=tmp_path)
            await restored._load_plan()
            dependent = restored._plan.step("step-dependent")
            assert dependent.ready_when == condition
            assert restored._plan.ready_state(
                dependent, isolate_steps=restored._execution.isolate_steps) is State(expected)
            for compact in (False, True):
                detail = await projects.task_detail("task-a", compact=compact)
                assert detail["steps"][1]["ready_state"] == expected
            pipeline = await PIPELINE_TOOL.run(SimpleNamespace(arguments={"task_id": "task-a"}))
            assert pipeline["steps"][1]["ready_state"] == expected

    run_db(test, monkeypatch, timeout=30)


def test_dependency_conditions_upgrade_is_named_and_idempotent(monkeypatch):
    from looper.db import connection

    async def test(pool):
        async with pool.acquire() as conn:
            assert not await connection.schema_change_pending(conn)
        await pool.execute("ALTER TABLE task_steps DROP COLUMN ready_when")
        await pool.execute("ALTER TABLE projects DROP COLUMN dependency_conditions")
        await pool.execute("DELETE FROM schema_migrations WHERE name = 'dependency_conditions'")
        constraint = await pool.fetchval(
            "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
            "WHERE conname = 'task_journal_kind_check' AND conrelid = 'task_journal'::regclass")
        assert ", 'condition_blocked'::text" in constraint
        await pool.execute("ALTER TABLE task_journal DROP CONSTRAINT task_journal_kind_check")
        await pool.execute("ALTER TABLE task_journal ADD CONSTRAINT task_journal_kind_check "
                           + constraint.replace(", 'condition_blocked'::text", ""))
        async with pool.acquire() as conn:
            assert await connection.schema_change_pending(conn)
        await connection.ensure_schema(pool)
        async with pool.acquire() as conn:
            assert not await connection.schema_change_pending(conn)
        assert await pool.fetchval("SELECT EXISTS (SELECT 1 FROM schema_migrations WHERE name = 'dependency_conditions')")
        await connection.ensure_schema(pool)
        assert await pool.fetchval("SELECT count(*) FROM schema_migrations WHERE name = 'dependency_conditions'") == 1
        constraint = await pool.fetchval(
            "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
            "WHERE conname = 'task_journal_kind_check' AND conrelid = 'task_journal'::regclass")
        assert "'profile_fallback'" in constraint
        assert "'condition_blocked'" in constraint
        assert await pool.fetchval("SELECT count(*) FROM information_schema.columns WHERE table_schema = current_schema() "
                                   "AND column_name IN ('ready_when', 'dependency_conditions')") == 2

    run_db(test, monkeypatch, timeout=30)


def test_wait_view_evaluates_a_cancelled_gates_passed_condition(monkeypatch):
    from looper.agents import projects
    from looper.db import plans, projects as db_projects, tasks

    async def test(pool):
        project = await db_projects.create_project(name="conditions", pool=pool)
        await tasks.create_task(task_id="task-a", requirement="Wait after approval", project_id=project["id"],
                                status="running", pool=pool)
        condition = {"step": "step-g1", "on": "passed"}
        await plans.save("task-a", plan={"plan_id": "plan-a", "status": "executing"}, steps=[
            {"id": "step-g1", "index": 0, "key": "g1", "kind": "gate", "instruction": "Approve",
             "status": "verified"},
            {"id": "step-w1", "index": 1, "key": "w1", "kind": "wait", "instruction": "Pause",
             "depends_on": ["step-g1"], "ready_when": condition}],
            gates=[{"step_id": "step-g1", "topic": "Approve", "state": "cancelled"}],
            waits=[{"step_id": "step-w1", "topic": "Pause", "state": "pending", "spec": {}}], pool=pool)
        detail = await projects.task_detail("task-a")
        assert detail["waits"][0]["ready_when"] == condition
        assert detail["waits"][0]["ready_state"] == "unsatisfiable"
        assert detail["gates"][0]["ready_state"] is None

        from looper.agents.ops.clients import PIPELINE_TOOL

        compact = await projects.task_detail("task-a", compact=True)
        pipeline = await PIPELINE_TOOL.run(SimpleNamespace(arguments={"task_id": "task-a"}))
        assert pipeline["waits"] == compact["waits"]
        assert pipeline["waits"][0]["ready_state"] == "unsatisfiable"
        assert pipeline["steps"][1]["ready_when_text"] == compact["steps"][1]["ready_when_text"]
        assert pipeline["steps"][1]["ready_state"] == "unsatisfiable"
        assert "ready_state" not in pipeline["steps"][0]
        assert "ready_when_text" not in pipeline["steps"][0]

    run_db(test, monkeypatch, timeout=30)
