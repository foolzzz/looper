"""Acceptance definitions and the operator-only project switch on disposable PostgreSQL."""
import json

import pytest

from looper.agents import config_transfer
from looper.agents.project_execution import AcceptanceChecksMode as Mode
from looper.agents.tiers.planner import PlannerAgent, TaskPlan
from looper.agents.tiers.planner_protocol import parse_plan
from looper.db import connection, plans, projects, tasks
from tests.db_support import DSN, run_db, v1_api

pytestmark = pytest.mark.skipif(not DSN, reason="requires disposable LOOPER_TEST_DATABASE_URL")


def fenced(value):
    return "```json\n" + json.dumps(value) + "\n```"


def test_operator_switch_roundtrip_and_agent_refusal(tmp_path, monkeypatch):
    async def run(pool):
        project = await projects.create_project(name="Example", pool=pool)
        async with v1_api(tmp_path, pool) as call:
            for action in ("get", "update"):
                args = {"project_id": project["id"]}
                if action == "update":
                    args["acceptance_checks"] = "network"
                status, answer = await call(f"acceptance_checks.{action}", args, client=True)
                assert status == 403, answer
            for mode in ("off", "local", "network", "off"):
                status, answer = await call("acceptance_checks.update", {
                    "project_id": project["id"], "acceptance_checks": mode})
                assert status == 200 and answer["result"] == {"acceptance_checks": mode}, answer
                status, answer = await call("acceptance_checks.get", {"project_id": project["id"]})
                assert answer["result"] == {"acceptance_checks": mode}
                stored = await projects.get_project(project["id"], pool=pool)
                assert stored["acceptance_checks"] == (None if mode == "off" else mode)
                status, answer = await call("project_admin.list")
                visible = answer["result"]["projects"][0]
                assert visible.get("acceptance_checks") == (None if mode == "off" else mode)
                if mode == "off":
                    assert "acceptance_checks" not in visible
            for value in (True, "on", "LOCAL", None):
                status, answer = await call("acceptance_checks.update", {
                    "project_id": project["id"], "acceptance_checks": value})
                assert status == 400, answer
            status, _ = await call("acceptance_checks.get", {"project_id": "missing"})
            assert status == 404
    run_db(run, monkeypatch)


def test_definitions_survive_planner_restart_and_replan_without_execution(tmp_path, monkeypatch):
    async def run(pool):
        project = await projects.create_project(name="Example", pool=pool)
        await projects.set_acceptance_checks(project["id"], "local", pool=pool)
        await tasks.create_task(task_id="task-checks", requirement="Report", goal_id="g", project_id=project["id"],
                                work_dir=str(tmp_path), correlation_id="corr", status="running", pool=pool)
        agent = PlannerAgent(task_id="task-checks", runtime_root=tmp_path)
        task_plan = TaskPlan(plan_id="p", goal_id="g", requirement="Report", work_dir=str(tmp_path), status="executing")
        await agent._refresh_project(task_plan)
        raw = {"id": "s1", "instruction": "Report", "acceptance_criteria": ["works"], "depends_on": [], "review": False,
               "checks": [{"id": "c1", "criterion": 1, "type": "command", "command": "touch should-not-exist"}]}
        specs = parse_plan(fenced({"steps": [raw]}), acceptance_checks=Mode.LOCAL).steps
        changed = agent._add_steps(task_plan, specs)
        await agent._save(task_plan, *changed, plan_row=True)
        restored = PlannerAgent(task_id="task-checks", runtime_root=tmp_path)
        await restored._load_plan()
        assert restored._plan.steps[0].checks == specs[0].checks
        detail = await tasks.get_task_with_steps("task-checks", pool=pool)
        assert detail["steps"][0]["checks"][0]["command"] == "touch should-not-exist"
        assert not (tmp_path / "should-not-exist").exists()
        await projects.set_acceptance_checks(project["id"], None, pool=pool)
        await restored._refresh_project(restored._plan)
        del raw["checks"]
        raw["instruction"] = "Report with updated wording"
        changed = restored._add_steps(restored._plan, restored._plan_parser()(fenced({"steps": [raw]})).steps)
        await restored._save(restored._plan, *changed)
        saved = (await plans.load("task-checks", pool=pool))["steps"][0]
        assert saved["instruction"] == raw["instruction"]
        assert saved["checks"] == [check.row() for check in specs[0].checks]
        view = "\n".join(restored._plan_lines(restored._plan))
        assert json.loads(view.split("  checks: ", 1)[1]) == saved["checks"]
        assert not (tmp_path / "should-not-exist").exists()
        # While enabled, omitting checks replaces the definitions with an empty set.
        await projects.set_acceptance_checks(project["id"], "local", pool=pool)
        await restored._refresh_project(restored._plan)
        changed = restored._add_steps(restored._plan, restored._plan_parser()(fenced({"steps": [raw]})).steps)
        await restored._save(restored._plan, *changed)
        assert "checks" not in (await plans.load("task-checks", pool=pool))["steps"][0]
    run_db(run, monkeypatch)


@pytest.mark.parametrize("mode,checks,restores", [
    ("network", [{"id": "c1", "criterion": 1, "type": "command", "command": "true", "network": True}], True),
    ("local", [{"id": "c1", "criterion": 1, "type": "command", "command": "true", "network": True}], False),
    (None, [{"id": "c1", "criterion": 1, "type": "command", "command": "true"}], False),
    (None, [], True),
    ("local", {"corrupt": "not a check list"}, False),
])
def test_restore_validates_saved_checks_against_current_permission(tmp_path, monkeypatch, caplog,
                                                                 mode, checks, restores):
    async def run(pool):
        project = await projects.create_project(name="Restore example", pool=pool)
        await projects.set_acceptance_checks(project["id"], mode, pool=pool)
        await tasks.create_task(task_id="task-restore", requirement="Report", project_id=project["id"], pool=pool)
        await plans.save("task-restore", plan={"plan_id": "p", "status": "executing"}, steps=[{
            "id": "step-restore", "index": 0, "key": "s1", "instruction": "Report",
            "acceptance_criteria": ["works"], "checks": checks}], pool=pool)
        agent = PlannerAgent(task_id="task-restore", runtime_root=tmp_path)
        await agent._load_plan()
        assert (agent._plan is not None) is restores
        if restores:
            assert [check.row() for check in agent._plan.steps[0].checks] == (
                [{**checks[0], "timeout_seconds": 300, "required": True, "protect": []}] if checks else [])
        else:
            assert "cannot read its saved plan" in caplog.text
        # Invalid definitions stay stored for operator recovery; restore never rewrites them.
        assert json.loads(await pool.fetchval("SELECT checks::text FROM task_steps WHERE id='step-restore'")) == checks
    run_db(run, monkeypatch)


@pytest.mark.parametrize("mode", ["off", "local", "network"])
def test_update_declares_high_risk_confirmation_without_writing(monkeypatch, mode):
    import asyncio
    from unittest.mock import AsyncMock
    from looper.agents import api_v1

    operation = api_v1.find("POST", "/api/v1/acceptance_checks/update")
    assert operation.risk == "high" and operation.confirms and operation.operator_only
    write = AsyncMock()
    monkeypatch.setattr(projects, "set_acceptance_checks", write)
    impact = asyncio.run(api_v1.domain_for(operation).impact(
        None, operation, {"project_id": "project-example", "acceptance_checks": mode}, api_v1.OPERATOR, {}))
    assert f"project-example's acceptance_checks to {mode}" in impact
    assert "no check is executed yet" in impact
    write.assert_not_awaited()


def test_additive_migration_preserves_rows_and_reopens_idempotently(monkeypatch):
    from looper.db import migrations
    async def run(pool):
        project = await projects.create_project(name="Kept", pool=pool)
        await tasks.create_task(task_id="task-kept", requirement="Kept", project_id=project["id"], pool=pool)
        await pool.execute("INSERT INTO task_steps (id, task_id, step_index, instruction) VALUES ('step-kept', 'task-kept', 0, 'Kept')")
        await pool.execute("ALTER TABLE projects DROP COLUMN acceptance_checks")
        await pool.execute("ALTER TABLE task_steps DROP COLUMN checks")
        before_project = await pool.fetchval("SELECT to_jsonb(p)::text FROM projects p WHERE id=$1", project["id"])
        before_step = await pool.fetchval("SELECT to_jsonb(s)::text FROM task_steps s WHERE id='step-kept'")
        async with pool.acquire() as conn:
            assert await connection.schema_change_pending(conn)
            await connection.ensure_schema(conn)
            assert not await connection.schema_change_pending(conn)
        assert (await projects.get_project(project["id"], pool=pool))["acceptance_checks"] is None
        assert await pool.fetchval("SELECT (to_jsonb(p)-'acceptance_checks')::text FROM projects p WHERE id=$1", project["id"]) == before_project
        assert await pool.fetchval("SELECT (to_jsonb(s)-'checks')::text FROM task_steps s WHERE id='step-kept'") == before_step
        assert await pool.fetchval("SELECT checks::text FROM task_steps WHERE id='step-kept'") == '[]'
        async with pool.acquire() as conn:
            await connection.ensure_schema(conn)
        assert "acceptance_checks" in {m.name for m in migrations.discover()}
        assert await pool.fetchval("SELECT EXISTS (SELECT 1 FROM schema_migrations WHERE name='acceptance_checks')")
    run_db(run, monkeypatch)


def test_switch_export_import_and_agent_project_patch_boundary(tmp_path, monkeypatch):
    async def run(pool):
        project = await projects.create_project(name="Export example", pool=pool)
        async with v1_api(tmp_path, pool) as call:
            status, answer = await call("project_admin.update", {
                "project_id": project["id"], "fields": {"acceptance_checks": "local"}}, client=True)
            assert status == 400, answer
            for mode in (None, "local", "network", None):
                await projects.set_acceptance_checks(project["id"], mode, pool=pool)
                async with pool.acquire() as conn:
                    exported = json.loads("".join([chunk async for chunk in config_transfer.export_chunks(
                        conn, include_projects=True, include_secrets=False)]))
                settings = exported["projects"][0]["execution"]
                if mode is None:
                    assert "acceptance_checks" not in settings
                else:
                    assert settings["acceptance_checks"] == mode
                # An omitted permission preserves the target; only an explicit operator value changes it.
                await projects.set_acceptance_checks(project["id"], "local" if mode is None else None, pool=pool)
                async with pool.acquire() as conn, conn.transaction():
                    plan = await config_transfer.import_config(conn, exported, apply=True)
                    assert not any(c.action is config_transfer.Action.SKIPPED for c in plan.changes if c.section == "projects")
                assert (await projects.get_project(project["id"], pool=pool))["acceptance_checks"] == ("local" if mode is None else mode)
    run_db(run, monkeypatch)


def test_import_rejects_invalid_permission_as_a_project_error(monkeypatch):
    async def run(pool):
        async with pool.acquire() as conn:
            plan = await config_transfer.import_config(conn, {"format": config_transfer.FORMAT,
                "projects": [{"name": "Invalid", "execution": {"acceptance_checks": "on"}}]}, apply=False)
        error = next(c for c in plan.changes if c.section == "projects")
        assert error.action is config_transfer.Action.SKIPPED and "acceptance_checks" in error.reason
    run_db(run, monkeypatch)


def test_default_off_import_preview_omits_permission(monkeypatch):
    async def run(pool):
        async with pool.acquire() as conn:
            preview = await config_transfer.import_config(conn, {"format": config_transfer.FORMAT,
                "projects": [{"name": "Default import"}]}, apply=False)
        assert "acceptance_checks" not in json.dumps(preview.view())
    run_db(run, monkeypatch)


def test_config_import_permission_is_operator_only_and_missing_keeps_target(tmp_path, monkeypatch):
    async def run(pool):
        project = await projects.create_project(name="Import example", pool=pool)
        await projects.set_acceptance_checks(project["id"], "network", pool=pool)
        async with v1_api(tmp_path, pool) as call:
            document = {"format": config_transfer.FORMAT, "projects": [{"name": project["name"],
                "execution": {"acceptance_checks": "off"}}]}
            status, answer = await call("config.import_preview", {"document": document}, client=True)
            assert status == 403 and answer["error"]["code"] == "forbidden", answer
            assert (await projects.get_project(project["id"], pool=pool))["acceptance_checks"] == "network"
        async with pool.acquire() as conn, conn.transaction():
            # A legacy or disabled export cannot silently revoke the operator's permission.
            await config_transfer.import_config(conn, {"format": config_transfer.FORMAT,
                "projects": [{"name": project["name"]}]}, apply=True)
            assert await conn.fetchval("SELECT acceptance_checks FROM projects WHERE id=$1", project["id"]) == "network"
            await config_transfer.import_config(conn, document, apply=True)
            assert await conn.fetchval("SELECT acceptance_checks FROM projects WHERE id=$1", project["id"]) is None
            document["projects"][0]["name"] = "Imported network"
            document["projects"][0]["execution"]["acceptance_checks"] = "network"
            await config_transfer.import_config(conn, document, apply=True)
            assert await conn.fetchval("SELECT acceptance_checks FROM projects WHERE name='Imported network'") == "network"
    run_db(run, monkeypatch)


def test_saved_permission_reaches_planner_after_project_refresh(tmp_path, monkeypatch):
    async def run(pool):
        project = await projects.create_project(name="Read settings", pool=pool)
        await tasks.create_task(task_id="task-settings", requirement="Report", project_id=project["id"], pool=pool)
        agent = PlannerAgent(task_id="task-settings", runtime_root=tmp_path)
        task_plan = TaskPlan(plan_id="p", goal_id="g", requirement="Report", work_dir=str(tmp_path))
        for mode in ("local", "network", None):
            await projects.set_acceptance_checks(project["id"], mode, pool=pool)
            await agent._refresh_project(task_plan)
            assert agent._execution.acceptance_checks is (Mode.OFF if mode is None else Mode(mode))
            assert (await agent._read_execution()).acceptance_checks is agent._execution.acceptance_checks
            text = fenced({"steps": [{"id": "s1", "instruction": "Report", "acceptance_criteria": ["works"],
                "depends_on": [], "review": False, "checks": [{"id": "c1", "criterion": 1,
                "type": "command", "command": "pytest tests/test_report.py"}]}]})
            if mode is None:
                from looper.agents.tiers.planner_protocol import ProtocolError
                with pytest.raises(ProtocolError, match="off"):
                    agent._plan_parser()(text)
            else:
                assert agent._plan_parser()(text).steps[0].checks[0].id == "c1"
    run_db(run, monkeypatch)
