"""Project layer inheritance, confirmed budgets and atomic allowed lists on PostgreSQL."""

import pytest

from looper.agents import agent_settings, projects as project_service
from looper.db import agent_setting_layers, projects, tasks
from tests.db_support import DSN, run_db
from tests.test_api_v1_auth import CLIENT, _bearer, _send
from tests.test_api_v1_auth import clients as clients
from tests.test_api_v1_global_role_settings_db import _configured, _post, _result
from tests.test_operator_auth import TOKEN
from tests.test_task_agents_db import _profiles

pytestmark = [pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set"),
              pytest.mark.usefixtures("clients")]


async def _project(pool):
    await _profiles(pool)
    return (await projects.create_project(name="Example", pool=pool))["id"]


def test_project_role_system_precedence_and_null_inheritance_readback(tmp_path, monkeypatch):
    async def run(pool):
        project_id = await _project(pool)
        await agent_settings.update_system({"harness_prompt": "System example", "run_budget_usd": 10,
            "env": [{"key": "BASE", "value": "system"}, {"key": "MODE", "value": "system"}]})
        await agent_settings.update_role("worker", {"harness_prompt": "Global worker", "run_budget_usd": 8,
                                                    "system_prompt_mode": "replace"})
        async with _configured(tmp_path) as server:
            status, body = await _post(server, "project_agent_settings/update", {"project_id": project_id,
                "values": {"harness_prompt": "Project example", "run_budget_usd": 4,
                           "env": [{"key": "MODE", "value": "project"}]}}, operator=True)
            result = _result("project_agent_settings.update", body)
            assert status == 200 and result["project"]["values"]["run_budget_usd"] == 4
            assert set(result["roles"]) == {"planner", "worker", "reviewer"}
            assert result["roles"]["worker"]["effective"]["harness_prompt"] == {
                "value": "Project example", "source": "project"}
            status, body = await _post(server, "project_agent_settings/update_role", {"project_id": project_id,
                "role": "worker", "values": {"harness_prompt": "Project worker", "run_budget_usd": 2,
                    "env": [{"key": "BASE", "value": None}]}})
            worker = _result("project_agent_settings.update_role", body)["roles"]["worker"]
            assert status == 200 and worker["effective"]["harness_prompt"] == {
                "value": "Project worker", "source": "role_project"}
            assert worker["effective"]["run_budget_usd"] == {"value": 2, "source": "role_project"}
            assert worker["effective"]["env"] == [{"key": "MODE", "value": "project", "source": "project"}]
            assert worker["effective"]["system_prompt_mode"] == "replace"
            status, body = await _post(server, "project_agent_settings/update_role", {"project_id": project_id,
                "role": "worker", "values": {"harness_prompt": None, "env": []}})
            worker = _result("project_agent_settings.update_role", body)["roles"]["worker"]
            assert status == 200 and worker["values"]["harness_prompt"] is None
            assert worker["effective"]["harness_prompt"]["source"] == "project"
            assert {entry["key"]: entry["value"] for entry in worker["effective"]["env"]} == {
                "BASE": "system", "MODE": "project"}
            status, body = await _post(server, "project_agent_settings/update", {"project_id": project_id,
                "values": {"harness_prompt": None}})
            assert status == 200 and body["result"]["roles"]["worker"]["effective"]["harness_prompt"] == {
                "value": "Global worker", "source": "role"}
            assert body["result"]["roles"]["planner"]["effective"]["harness_prompt"] == {
                "value": "System example", "source": "system"}
            status, body = await _post(server, "project_agent_settings/get", {"project_id": project_id})
            assert status == 200 and _result("project_agent_settings.get", body) == await agent_settings.project_settings_view(project_id)
    run_db(run, monkeypatch, timeout=30)


@pytest.mark.parametrize("path,fields", [
    ("project_agent_settings/get", {}), ("project_agent_settings/update", {"values": {"harness_prompt": "Example"}}),
    ("project_agent_settings/update_role", {"role": "worker", "values": {"harness_prompt": "Example"}}),
    ("allowed_profiles/get", {}), ("allowed_profiles/replace", {"profile_ids": []}),
])
def test_unknown_project_returns_not_found(tmp_path, monkeypatch, path, fields):
    async def run(pool):
        async with _configured(tmp_path) as server:
            status, body = await _post(server, path, {"project_id": "proj-unknown", **fields}, operator=True)
            assert status == 404 and body["error"]["code"] == "not_found"
    run_db(run, monkeypatch, timeout=30)


@pytest.mark.parametrize("fields", [
    {"role": "manager", "values": {"harness_prompt": "Must not save"}},
    {"values": {"max_concurrency": 2}}, {"role": "worker", "values": {"max_concurrency": 2}},
])
def test_project_manager_role_and_concurrency_are_rejected_without_writes(tmp_path, monkeypatch, fields):
    async def run(pool):
        project_id = await _project(pool)
        before = await agent_settings.project_settings_view(project_id)
        async with _configured(tmp_path) as server:
            path = "project_agent_settings/update_role" if "role" in fields else "project_agent_settings/update"
            status, body = await _post(server, path, {"project_id": project_id, **fields}, operator=True)
            assert status == 400 and body["error"]["code"] == "invalid"
            assert await agent_settings.project_settings_view(project_id) == before
            server._worker_pool.recheck.assert_not_awaited()
    run_db(run, monkeypatch, timeout=30)


@pytest.mark.parametrize("role", [None, "worker"])
@pytest.mark.parametrize("budget", [6, None], ids=["raised", "lifted"])
def test_budget_raise_or_lift_waits_for_client_confirmation_but_operator_executes(tmp_path, monkeypatch, role, budget):
    async def run(pool):
        project_id = await _project(pool)
        if budget is not None:
            await agent_settings.update_system({"run_budget_usd": 10})
        parent_budget = 4 if role is None or budget is not None else None
        await agent_settings.update_project(project_id, {"run_budget_usd": parent_budget})
        if role:
            await agent_settings.update_project(project_id, {"run_budget_usd": 2}, role)
        before = await agent_settings.project_settings_view(project_id)
        path = "project_agent_settings/update_role" if role else "project_agent_settings/update"
        data = {"project_id": project_id, "values": {"run_budget_usd": budget}, **({"role": role} if role else {})}
        async with _configured(tmp_path) as server:
            status, pending = await _post(server, path, data)
            assert status == 202 and pending["status"] == "needs_confirmation" and pending["impact"]
            assert await agent_settings.project_settings_view(project_id) == before
            server._worker_pool.recheck.assert_not_awaited()
            status, body = await _post(server, "confirm", {"confirmation_id": pending["confirmation_id"]})
            result = _result(path.replace("/", "."), body)
            layer = result["roles"][role] if role else result["project"]
            assert status == 200 and layer["values"]["run_budget_usd"] == budget
            assert result["roles"][role or "planner"]["effective"]["run_budget_usd"]["value"] == budget
            trail = await pool.fetch("SELECT status, risk, client_id, confirmation_id FROM manager_actions "
                                     "WHERE tool=$1 ORDER BY id", path.replace("/", "."))
            assert [(row["status"], row["risk"], row["client_id"], row["confirmation_id"]) for row in trail] == [
                (state, "high", CLIENT["id"], pending["confirmation_id"]) for state in ("needs_confirmation", "ok")]
            await agent_settings.update_project(project_id, {"run_budget_usd": 2 if role else parent_budget}, role)
            status, body = await _post(server, path, data, operator=True)
            assert status == 200 and body["status"] == "ok"
            operator = await pool.fetchrow("SELECT status, risk, client_id, confirmation_id FROM manager_actions "
                                           "ORDER BY id DESC LIMIT 1")
            assert dict(operator) == {"status": "ok", "risk": "high", "client_id": None, "confirmation_id": None}
            server._worker_pool.recheck.assert_not_awaited()
    run_db(run, monkeypatch, timeout=30)


@pytest.mark.parametrize("role", [None, "worker"], ids=["overridden-project", "unchanged-project-role"])
def test_budget_update_without_an_effective_increase_does_not_require_confirmation(tmp_path, monkeypatch, role):
    async def run(pool):
        project_id = await _project(pool)
        await agent_settings.update_project(project_id, {"run_budget_usd": 4})
        for task_role in ("planner", "worker", "reviewer"):
            await agent_settings.update_project(project_id, {"run_budget_usd": 2}, task_role)
        async with _configured(tmp_path) as server:
            path = "project_agent_settings/update_role" if role else "project_agent_settings/update"
            status, body = await _post(server, path, {"project_id": project_id,
                "values": {"run_budget_usd": 2 if role else 6}, **({"role": role} if role else {})})
            result = _result(path.replace("/", "."), body)
            assert status == 200 and body["status"] == "ok"
            assert all(state["effective"]["run_budget_usd"]["value"] == 2 for state in result["roles"].values())
            assert await pool.fetchval("SELECT count(*) FROM manager_actions WHERE status='needs_confirmation'") == 0
    run_db(run, monkeypatch, timeout=30)


def test_allowed_profile_replacement_removes_previous_choices_and_empty_is_unrestricted(tmp_path, monkeypatch):
    async def run(pool):
        project_id = await _project(pool)
        await tasks.create_task(task_id="task-profile-choice", requirement="Build example", project_id=project_id,
                                work_dir="", status="running", pool=pool)
        async with _configured(tmp_path) as server:
            for wanted, chosen, retained in [(["agent-sonnet"], "agent-sonnet", "agent-sonnet"),
                (["agent-one-off"], "agent-sonnet", None), ([], "agent-one-off", "agent-one-off")]:
                await agent_setting_layers.set_planner_profile("task-profile-choice", "worker", None, chosen,
                    {"step_id": "", "kind": "profile_choice", "summary": "Choose example profile", "learnings": []}, pool=pool)
                status, body = await _post(server, "allowed_profiles/replace", {"project_id": project_id,
                    "profile_ids": wanted})
                result = _result("allowed_profiles.replace", body)
                assert status == 200 and result["restricted"] == bool(wanted)
                assert [profile["profile_id"] for profile in result["profiles"]] == wanted
                assert [profile["id"] for profile in await projects.allowed_profiles(project_id)] == wanted
                assert result["choosable"] and all(profile["roles"] for profile in result["choosable"])
                assert result == await project_service.allowed_profiles_view(project_id)
                assert await pool.fetchval("SELECT profile_id FROM task_planner_profiles WHERE task_id=$1",
                                           "task-profile-choice") == retained
                choices = await agent_setting_layers.planner_profiles("task-profile-choice", pool=pool)
                assert [choice["profile"]["id"] for choice in choices] == ([retained] if retained else [])
            status, body = await _post(server, "allowed_profiles/get", {"project_id": project_id})
            assert status == 200 and _result("allowed_profiles.get", body) == result
            assert {"agent-sonnet", "agent-one-off"} <= {profile["profile_id"] for profile in result["choosable"]}
    run_db(run, monkeypatch, timeout=30)


@pytest.mark.parametrize("invalid", ["agent-unknown", "agent-one-off"])
def test_invalid_or_incompatible_profile_rejects_the_entire_replacement(tmp_path, monkeypatch, invalid):
    async def run(pool):
        project_id = await _project(pool)
        await projects.set_allowed_profiles(project_id, ["agent-sonnet"], pool=pool)
        await pool.execute("INSERT INTO agent_profiles (id, name, runtime_id, model_id) "
                           "SELECT 'agent-replacement', 'Replacement', runtime_id, model_id "
                           "FROM agent_profiles WHERE id='agent-sonnet'")
        if invalid == "agent-one-off":
            await pool.execute("UPDATE agent_runtimes SET capabilities = '{}' WHERE id='rt-one-off'")
        before = await project_service.allowed_profiles_view(project_id)
        async with _configured(tmp_path) as server:
            status, body = await _post(server, "allowed_profiles/replace", {"project_id": project_id,
                "profile_ids": ["agent-replacement", invalid]})
            assert status == 400 and body["error"]["code"] == "invalid"
            assert await project_service.allowed_profiles_view(project_id) == before
    run_db(run, monkeypatch, timeout=30)


def test_existing_add_remove_operation_keeps_compact_schema_and_semantics(tmp_path, monkeypatch):
    async def run(pool):
        project_id = await _project(pool)
        await projects.set_allowed_profiles(project_id, ["agent-sonnet"], pool=pool)
        async with _configured(tmp_path) as server:
            status, body = await _post(server, "ops/settings/allowed_profiles", {"project": "Example",
                "add": ["agent-one-off"], "remove": ["claude-code/sonnet"]})
            result = _result("settings.allowed_profiles", body)
            assert status == 200 and result["project"] == "Example" and result["before"] == ["claude-code/sonnet"]
            assert [profile["profile_id"] for profile in result["allowed_profiles"]] == ["agent-one-off"]
            assert set(result) == {"project", "before", "allowed_profiles", "note"}
            assert [profile["id"] for profile in await projects.allowed_profiles(project_id)] == ["agent-one-off"]
            status, body = await _post(server, "ops/settings/allowed_profiles", {"project": "Example",
                "remove": ["agent-sonnet"]})
            assert status == 404 and body["error"]["code"] == "not_found"
            assert [profile["id"] for profile in await projects.allowed_profiles(project_id)] == ["agent-one-off"]
    run_db(run, monkeypatch, timeout=30)


@pytest.mark.parametrize("method,suffix,path,fields", [
    ("GET", "agent-settings", "project_agent_settings/get", {}),
    ("PUT", "agent-settings", "project_agent_settings/update", {"values": {"harness_prompt": None}}),
    ("PUT", "agent-settings/roles/worker", "project_agent_settings/update_role", {"role": "worker", "values": {"env": []}}),
    ("GET", "allowed-profiles", "allowed_profiles/get", {}),
    ("PUT", "allowed-profiles", "allowed_profiles/replace", {"profile_ids": ["agent-sonnet"]}),
])
def test_retired_project_routes_are_404_with_authenticated_v1_controls(tmp_path, monkeypatch, method, suffix, path, fields):
    async def run(pool):
        project_id = await _project(pool)
        async with _configured(tmp_path) as server:
            status, body = await _post(server, path, {"project_id": project_id, **fields}, operator=True)
            assert status == 200 and body["status"] == "ok"
            status, _, _ = await _send(server, f"{method} /api/projects/{project_id}/{suffix}", _bearer(TOKEN), "{}")
            assert status == 404
    run_db(run, monkeypatch, timeout=30)
