"""Planner profile choices must agree with the settings a step's next run loads."""

from types import SimpleNamespace
from typing import Any

import pytest

from looper.agents.agent_settings import load_agent_settings
from looper.agents.ops.planner import PlannerOps
from looper.db import agent_setting_layers, projects, tasks
from tests.db_support import DSN, run_db

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")


@pytest.mark.parametrize(("layer", "kind", "role"), [
    ("task_role", "work", "worker"),
    ("task_role", "review", "reviewer"),
    ("task", "work", "worker"),
])
def test_profile_choice_refuses_a_pin_then_applies_without_it(
    monkeypatch: pytest.MonkeyPatch, layer: str, kind: str, role: str,
) -> None:
    async def test(pool: Any) -> None:
        project = await projects.create_project(name="profile choice")
        await tasks.create_task(task_id="task-a", requirement="Do the step", project_id=project["id"],
                                status="running")
        await pool.execute(
            "INSERT INTO agent_profiles (id, name, runtime_id, model_id) VALUES "
            "('agent-sonnet', 'claude-code/sonnet', 'rt-claude-code', "
            "(SELECT id FROM llm_models WHERE provider_id = 'prov-claude-subscription' AND name = 'sonnet'))")
        await pool.execute(
            "INSERT INTO task_steps (id, task_id, step_index, step_key, instruction, status, kind) "
            "VALUES ('step-a', 'task-a', 0, 'a', 'Do it', 'pending', $1)", kind)
        pinned = "agent-claude-code-opus"
        task_role = None
        if layer == "task_role":
            # A new attempt uses the latest definition, not the previous attempt's version.
            await pool.execute(
                "INSERT INTO task_roles (task_id, role_key, version, kind, role_prompt, profile_id, via) "
                "VALUES ('task-a', 'specialist', 1, $1, 'Do the work', NULL, 'workflow'), "
                "('task-a', 'specialist', 2, $1, 'Do the work', $2, 'workflow')", kind, pinned)
            await pool.execute("UPDATE task_steps SET role_key = 'specialist', role_version = 1")
            task_role = ("specialist", 2)
        else:
            await agent_setting_layers.set_task_layer("task-a", profiles={role: pinned})

        ops = PlannerOps(SimpleNamespace(), url="http://127.0.0.1:9", dashboard_url="http://looper.test")
        token = ops.issue("task-a")
        ops.begin_turn(token)
        arguments = {"role": role, "profile_id": "agent-sonnet", "step": "a"}

        async def next_run() -> dict[str, str]:
            settings = await load_agent_settings(role, project["id"], "task-a", step_id="step-a",
                                                 task_role=task_role)
            return {"name": settings.profile.name, "source": settings.profile_source.value}

        before = await next_run()
        assert before == {"name": "claude-code/opus", "source": layer}
        answer = await ops.call(f"Bearer {token}", "looper_task_profile_set", arguments)
        assert answer["status"] == "error", answer
        assert answer["error"]["code"] == "forbidden"
        message = answer["error"]["message"]
        assert f"{layer} layer" in message and before["name"] in message
        assert "dashboard" in message and "user gate" in message
        if layer == "task_role":
            assert "Roles" in message
        assert await next_run() == before
        assert await agent_setting_layers.planner_profiles("task-a") == []
        assert await pool.fetchval("SELECT count(*) FROM task_journal WHERE kind = 'profile_choice'") == 0
        assert not await pool.fetchval("SELECT EXISTS (SELECT 1 FROM activity "
                                       "WHERE summary LIKE 'Planner chose the Agent Profile:%')")

        # Removing the operator pin makes the same call effective; clearing it resolves inheritance too.
        await pool.execute("UPDATE task_steps SET role_key = NULL, role_version = NULL")
        task_role = None
        await agent_setting_layers.set_task_layer("task-a", profiles={role: None})
        answer = await ops.call(f"Bearer {token}", "looper_task_profile_set", arguments)
        assert answer["status"] == "ok", answer
        assert answer["result"]["changed"] is True
        assert answer["result"]["next_run"] == await next_run() == {
            "name": "claude-code/sonnet", "source": "planner_step"}
        answer = await ops.call(f"Bearer {token}", "looper_task_profile_set", {**arguments, "profile_id": None})
        assert answer["status"] == "ok", answer
        assert answer["result"]["next_run"] == await next_run() == {
            "name": "claude-code/opus", "source": "builtin"}

    run_db(test, monkeypatch, timeout=30)
