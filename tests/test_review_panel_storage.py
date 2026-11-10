"""Review-panel persistence and task-facing acceptance state."""
import json
from types import SimpleNamespace

import pytest

from tests.db_support import DSN, run_db

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")


def test_review_panel_persists_acceptance_and_task_readback(monkeypatch):
    from looper.agents import api_v1, projects
    from looper.agents.ops.clients import PIPELINE_TOOL
    from looper.agents.ops.service import _plain
    from looper.db import journal, plans, projects as db_projects, tasks

    async def test(pool):
        project = await db_projects.create_project(name="review-panel", pool=pool)
        await tasks.create_task(task_id="task-a", requirement="Review a change", project_id=project["id"],
                                status="running", pool=pool)
        accept_when = {"step": "step-r1", "on": "passed"}
        await plans.save("task-a", plan={"plan_id": "plan-a", "status": "executing"}, steps=[
            {"id": "step-s1", "index": 0, "key": "s1", "instruction": "Change", "status": "completed",
             "result": {"status": "completed"}, "accept_when": accept_when},
            {"id": "step-r1", "index": 1, "key": "s1/review_a", "kind": "review", "instruction": "Review",
             "reviews": "step-s1", "depends_on": ["step-s1"],
             "ready_when": {"step": "step-s1", "on": "completed"}}], pool=pool)
        stored = await plans.load("task-a", pool=pool)
        assert stored["steps"][0]["accept_when"] == accept_when
        assert stored["steps"][1]["reviews"] == "step-s1"
        detail = _plain(await projects.task_detail("task-a"))
        api_v1.validate_arguments(api_v1.find("POST", "/api/v1/tasks/detail").result, detail)
        assert detail["steps"][0]["accept_state"] == "pending"
        assert detail["steps"][1]["ready_state"] == "satisfied"
        await pool.execute("UPDATE task_steps SET status='verified', result=$1::jsonb WHERE id='step-r1'",
                           '{"passed": false}')
        detail = await projects.task_detail("task-a")
        assert detail["steps"][0]["accept_state"] == "unsatisfiable"
        await pool.execute("UPDATE task_steps SET result=$1::jsonb WHERE id='step-r1'",
                           '{"passed": true, "status": "passed_by_adjudication"}')
        detail = await projects.task_detail("task-a", compact=True)
        assert detail["steps"][0]["accept_state"] == "satisfied"
        assert detail["steps"][0]["accept_when_text"] == "s1/review_a passed"
        pipeline = await PIPELINE_TOOL.run(SimpleNamespace(arguments={"task_id": "task-a"}))
        assert pipeline["steps"][0]["accept_state"] == "satisfied"
        assert pipeline["steps"][1]["reviews"] == "s1"
        for kind in ("review_round", "late_review", "review_adjudicated"):
            await journal.insert(pool, "task-a", [{"step_id": "step-s1", "kind": kind, "summary": kind}])
        assert {entry["kind"] for entry in await journal.list_entries("task-a", pool=pool)} == {
            "review_round", "late_review", "review_adjudicated"}

    run_db(test, monkeypatch, timeout=30)


@pytest.mark.parametrize("distinct", [None, False, True])
def test_review_panel_upgrade_is_named_and_idempotent(tmp_path, monkeypatch, distinct):
    from looper.db import connection
    from looper.db import projects, tasks
    from tests.db_support import v1_api

    async def test(pool):
        legacy = {"review_panel": {"members": ["builtin"], "accept_when": "builtin"}}
        if distinct is not None:
            legacy["distinct_reviewer_runtime"] = distinct
        await pool.execute("INSERT INTO workflows (key, name, definition) VALUES ('legacy', 'Legacy', $1::jsonb), "
                           "('plain', 'Plain', '{\"distinct_reviewer_runtime\": false}')", json.dumps(legacy))
        project = await projects.create_project(name="Legacy project", work_dir=str(tmp_path),
                                                execution={"task_workspaces": "in_place"})
        await pool.execute("UPDATE projects SET default_workflow='legacy' WHERE id=$1", project["id"])
        await tasks.create_task(task_id="old-task", requirement="Existing task", project_id=project["id"],
                                workflow=legacy)
        await pool.execute("ALTER TABLE task_steps DROP COLUMN accept_when, DROP COLUMN reviews")
        await pool.execute("DELETE FROM schema_migrations WHERE name='review_panel'")
        await pool.execute("ALTER TABLE task_steps DROP CONSTRAINT task_steps_kind_check")
        await pool.execute("ALTER TABLE task_steps ADD CONSTRAINT task_steps_kind_check "
                           "CHECK (kind IN ('work', 'gate', 'wait'))")
        async with pool.acquire() as conn:
            assert await connection.schema_change_pending(conn)
        await connection.ensure_schema(pool)
        await connection.ensure_schema(pool)
        assert await pool.fetchval("SELECT count(*) FROM schema_migrations WHERE name='review_panel'") == 1
        async with pool.acquire() as conn:
            assert not await connection.schema_change_pending(conn)
        assert await pool.fetchval("SELECT count(*) FROM information_schema.columns "
                                   "WHERE table_schema=current_schema() AND table_name='task_steps' "
                                   "AND column_name IN ('accept_when', 'reviews')") == 2
        constraints = await pool.fetch("SELECT pg_get_constraintdef(oid) AS definition FROM pg_constraint "
                                       "WHERE conname IN ('task_steps_kind_check', 'task_journal_kind_check') "
                                       "AND connamespace=current_schema()::regnamespace")
        text = " ".join(row["definition"] for row in constraints)
        assert all(f"'{kind}'" in text for kind in ("review", "review_round", "late_review", "review_adjudicated"))
        assert json.loads(await pool.fetchval("SELECT definition FROM workflows WHERE key='legacy'")) == {
            **legacy, "distinct_reviewer_runtime": True}
        assert json.loads(await pool.fetchval("SELECT definition FROM workflows WHERE key='plain'")) == {
            "distinct_reviewer_runtime": False}
        assert (await tasks.get_task("old-task"))["workflow"] == legacy
        async with v1_api(tmp_path, pool) as call:
            status, answer = await call("workflows.list")
            assert status == 200, answer
            assert [row["key"] for row in answer["result"]["workflows"]] == ["legacy", "plain"]
            status, answer = await call("workflows.get", {"key": "legacy"})
            assert status == 200 and answer["result"]["workflow"]["distinct_reviewer_runtime"] is True
            status, answer = await call("tasks.create", {"project_id": project["id"], "requirement": "Build"})
            assert status == 200, answer
            snapshot = (await tasks.get_task(answer["result"]["task_id"]))["workflow"]
            assert snapshot["key"] == "legacy" and snapshot["distinct_reviewer_runtime"] is True

    run_db(test, monkeypatch, timeout=30)
