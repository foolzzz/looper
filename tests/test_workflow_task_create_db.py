"""Task creation snapshots workflow execution choices atomically at the API entry point."""
from types import SimpleNamespace

import pytest

from looper.agents import api_v1
from looper.agents.ops.planner import PlannerOps
from looper.agents.projects import RequestError, create_task_core
from looper.agents.project_execution import ProjectExecution
from looper.db import agent_setting_layers, projects, task_roles, tasks
from tests.db_support import DSN, run_db, v1_api

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")

PRESET = {"key": "reviewed", "name": "Reviewed", "planner_instructions": "Keep changes focused.",
          "roles": [{"key": "dev", "kind": "work", "role_prompt": "Implement the change.",
                     "profile_id": "workflow-profile"}], "work_role": "dev",
          "profiles": {"worker": "workflow-profile"}, "budget_usd": 2.5,
          "max_step_retries": 1, "review_all": True, "dependency_conditions": True,
          "escalate_on_exhaustion": True}


async def setup(pool, call, tmp_path):
    await pool.execute("INSERT INTO agent_profiles (id, name, runtime_id, model_id) "
                       "SELECT 'workflow-profile', 'Workflow profile', 'rt-claude-code', id "
                       "FROM llm_models WHERE provider_id = 'prov-claude-subscription' AND name = 'sonnet'")
    status, answer = await call("workflows.put", PRESET)
    assert status == 200, answer
    project = await projects.create_project(name="Example", work_dir=str(tmp_path),
                                            execution={"task_workspaces": "in_place"})
    await pool.execute("UPDATE projects SET default_workflow = 'reviewed' WHERE id = $1", project["id"])
    return project


def test_task_creation_preserves_workflow_roles_profiles_budget_and_effective_execution(tmp_path, monkeypatch):
    async def test(pool):
        async with v1_api(tmp_path, pool) as call:
            project = await setup(pool, call, tmp_path)
            status, answer = await call("tasks.create", {"project_id": project["id"], "requirement": "Build",
                                                        "workflow": "reviewed"})
            assert status == 200, answer
            task_id = answer["result"]["task_id"]
            row = await tasks.get_task(task_id)
            assert row["workflow"]["key"] == "reviewed" and row["budget_usd"] == 2.5
            role = (await task_roles.current(task_id))[0]
            assert (role["role_key"], role["via"], role["version"], role["profile_id"]) == (
                "dev", "workflow", 1, "workflow-profile")
            assert (await agent_setting_layers.profile_overrides(task_id))["worker"]["id"] == "workflow-profile"
            current = await projects.get_task_project(task_id)
            execution = ProjectExecution.from_row(current).overlaid(current["task_workflow"])
            assert execution.step_retries == 1 and execution.review_all and execution.conditions_on
            assert execution.gates_on and execution.task_budget_usd == 2.5
            # Replacement changes future selections, and start_over restores the original role version.
            assert (await call("workflows.put", {"key": "reviewed", "name": "Updated"}))[0] == 200
            await pool.execute("UPDATE tasks SET status = 'failed' WHERE id = $1", task_id)
            assert await tasks.reset_task_for_restart(task_id, keep_steps=False)
            assert (await task_roles.current(task_id))[0]["role_prompt"] == "Implement the change."
            assert (await tasks.get_task(task_id))["workflow"] == row["workflow"]
            status, answer = await call("tasks.detail", {"task_id": task_id})
            assert status == 200 and answer["result"]["workflow"] == row["workflow"], answer
            api_v1.validate_arguments(api_v1.find("POST", "/api/v1/tasks/detail").result, answer["result"])
    run_db(test, monkeypatch, timeout=45)


@pytest.mark.parametrize("selection,expected", [(None, "reviewed"), ("none", None)])
def test_project_default_is_used_unless_explicitly_disabled(tmp_path, monkeypatch, selection, expected):
    async def test(pool):
        async with v1_api(tmp_path, pool) as call:
            project = await setup(pool, call, tmp_path)
            body = {"project_id": project["id"], "requirement": "Build"}
            if selection is not None:
                body["workflow"] = selection
            status, answer = await call("tasks.create", body)
            assert status == 200, answer
            snapshot = (await tasks.get_task(answer["result"]["task_id"]))["workflow"]
            assert (snapshot or {}).get("key") == expected
    run_db(test, monkeypatch, timeout=30)


def test_conflicting_profile_is_invalid_and_creates_nothing(tmp_path, monkeypatch):
    async def test(pool):
        async with v1_api(tmp_path, pool) as call:
            project = await setup(pool, call, tmp_path)
            await pool.execute("INSERT INTO agent_profiles (id, name, runtime_id, model_id) "
                               "SELECT 'other-profile', 'Other', runtime_id, model_id FROM agent_profiles "
                               "WHERE id = 'workflow-profile'")
            status, answer = await call("tasks.create", {"project_id": project["id"], "requirement": "Build",
                "profile_overrides": {"worker": "other-profile"}})
            assert status == 400 and answer["error"]["code"] == "invalid", answer
            assert await pool.fetchval("SELECT count(*) FROM tasks") == 0
    run_db(test, monkeypatch, timeout=30)


def test_template_excludes_workflow_selection_and_project_default(tmp_path, monkeypatch):
    async def test(pool):
        async with v1_api(tmp_path, pool) as call:
            project = await setup(pool, call, tmp_path)
            data = {"project_id": project["id"], "requirement": "Build"}
            with pytest.raises(RequestError, match="workflow and template"):
                await create_task_core(data, workflow="none", task_fields={"template": {"key": "report"}})
            result = await create_task_core(data, task_fields={"template": {"key": "report"}})
            assert (await tasks.get_task(result["task_id"]))["workflow"] is None
    run_db(test, monkeypatch, timeout=30)


def test_planner_cannot_replace_workflow_role(tmp_path, monkeypatch):
    async def test(pool):
        async with v1_api(tmp_path, pool) as call:
            project = await setup(pool, call, tmp_path)
            status, answer = await call("tasks.create", {"project_id": project["id"], "requirement": "Build"})
            assert status == 200, answer
            task_id = answer["result"]["task_id"]
            await pool.execute("UPDATE tasks SET status = 'running' WHERE id = $1", task_id)
            await pool.execute("UPDATE projects SET planner_roles = 'on' WHERE id = $1", project["id"])
            ops = PlannerOps(SimpleNamespace(), url="http://127.0.0.1:9", dashboard_url="http://looper.test")
            token = ops.issue(task_id)
            ops.begin_turn(token)
            answer = await ops.call(f"Bearer {token}", "looper_task_role_set", {
                "id": "dev", "kind": "work", "role_prompt": "Replace the role."})
            assert answer["status"] == "error" and answer["error"]["code"] == "forbidden", answer
            assert len(await task_roles.versions(task_id)) == 1
    run_db(test, monkeypatch, timeout=30)


def test_fixed_workflow_role_profile_runs_when_project_restricts_planner_choices(tmp_path, monkeypatch):
    from looper.agents.agent_settings import load_agent_settings

    async def test(pool):
        async with v1_api(tmp_path, pool) as call:
            project = await setup(pool, call, tmp_path)
            assert (await call("workflows.put", {**PRESET, "profiles": {}}))[0] == 200
            await pool.execute("INSERT INTO project_allowed_profiles (project_id, profile_id) "
                               "VALUES ($1, 'agent-claude-code-opus')", project["id"])
            status, answer = await call("tasks.create", {"project_id": project["id"], "requirement": "Build"})
            assert status == 200, answer
            task_id = answer["result"]["task_id"]
            settings = await load_agent_settings("worker", project["id"], task_id, task_role=("dev", 1))
            assert settings.profile.id == "workflow-profile"
            assert (await task_roles.current(task_id))[0]["profile_allowed"]
    run_db(test, monkeypatch, timeout=30)
