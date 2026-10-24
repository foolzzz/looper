"""Authenticated task creation, confirmation isolation and persisted state on disposable PostgreSQL."""

import json
import subprocess
from types import SimpleNamespace

import pytest

from looper.agents import api_v1
from looper.agents.ops.clients import ClientOps
from looper.db import agent_setting_layers, api_clients, manager_actions, projects, tasks
from tests.db_support import DSN, run_db
from tests.test_api_v1_auth import _bearer, _send, _serving
from tests.test_operator_auth import TOKEN
from tests.test_ops_tasks_db import _Chat

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")
PATH = "/api/v1/tasks/create"


async def post(server, body, *, token=TOKEN, session="create", path=PATH):
    code, _, answer = await _send(server, f"POST {path}", {**_bearer(token), "X-Looper-Session": session},
                                  json.dumps(body))
    return code, answer


def repository(path):
    path.mkdir()
    for args in (("init", "-q"), ("-c", "user.name=Test", "-c", "user.email=test@example.invalid",
                                 "-c", "core.hooksPath=/dev/null", "commit", "--allow-empty", "-qm", "Initial")):
        subprocess.run(["git", "-C", str(path), *args], check=True, capture_output=True, timeout=10)
    return str(path)


@pytest.mark.parametrize("origin", ["dashboard", "manager", "client"])
@pytest.mark.parametrize("new_project", [False, True], ids=["existing-project", "new-project"])
def test_only_manager_and_client_creates_enter_the_action_trail(monkeypatch, tmp_path, origin, new_project):
    async def test(pool):
        client, token = await api_clients.create("Client", pool=pool)
        as_client = origin == "client"
        choice = ({"project": {"name": "Example", "work_dir": str(tmp_path)}} if new_project else
                  {"project_id": (await projects.create_project(
                      name="Example", work_dir=str(tmp_path), pool=pool))["id"]})
        async with _serving(tmp_path) as server:
            server._client_ops = ClientOps(SimpleNamespace())
            if origin == "manager":
                chat = _Chat(tmp_path)
                (answer,) = await chat.say("Create a task", ("looper_task_create", {
                    "requirement": "Build", "title": "Build", "project": "Example", "work_dir": str(tmp_path)}))
                if new_project:
                    assert answer["status"] == "needs_confirmation", answer
                    (answer,) = await chat.say("Confirm", ("looper_confirm", {
                        "confirmation_id": answer["confirmation_id"]}))
                assert answer["status"] == "ok", answer
            else:
                code, answer = await post(server, {"requirement": "Build", **choice},
                                          token=token if as_client else TOKEN)
                if as_client and new_project:
                    assert code == 202, answer
                    code, answer = await post(server, {"confirmation_id": answer["confirmation_id"]},
                                              token=token, path="/api/v1/confirm")
                assert code == 200, answer
            task_id = answer["result"]["task_id"]
            row = await tasks.get_task(task_id)
            assert row["manager_id"] == "manager"  # Task ownership is separate from who performed the action.
            assert row["client_id"] == (client["id"] if as_client else None)
            trail = await manager_actions.for_task(task_id, pool=pool)
            tool = "looper_task_create" if origin == "manager" else "tasks.create"
            client_id = client["id"] if as_client else None
            assert [(a["tool"], a["status"], a["client_id"]) for a in trail] == (
                [(tool, "ok", client_id)] if origin != "dashboard" else [])
            actions = await pool.fetch("SELECT tool, status, manager_id, client_id, channel "
                                       "FROM manager_actions ORDER BY id")
            statuses = ["needs_confirmation", "ok"] if new_project else ["ok"]
            assert [(a["tool"], a["status"], a["manager_id"], a["client_id"], a["channel"]) for a in actions] == (
                [(tool, status, "manager", client_id, "api" if as_client else "lark") for status in statuses]
                if origin != "dashboard" else [])
    run_db(test, monkeypatch, timeout=60)


@pytest.mark.parametrize("new_project", [False, True])
@pytest.mark.parametrize("mode", [None, "in_place"])
def test_full_configuration_and_workspace_choice_survive_readback(monkeypatch, tmp_path, new_project, mode):
    work = repository(tmp_path / "workspace")

    async def test(pool):
        await pool.execute("INSERT INTO agent_profiles (id, name, runtime_id, model_id) VALUES "
                           "('profile-choice', 'Choice', 'rt-claude-code', "
                           "(SELECT id FROM llm_models WHERE provider_id = 'prov-claude-subscription' "
                           "AND name = 'sonnet'))")
        project = {"name": "Example", "work_dir": work, "description": "Description", "background": "Background",
                   "task_budget_usd": 12, "task_workspaces": "worktree"}
        choice = {"project": project} if new_project else {"project_id": (await projects.create_project(
            name="Example", work_dir=work, execution={"task_workspaces": "worktree"}, pool=pool))["id"]}
        config = {"nested": {"enabled": False, "items": [1, "two"]}, "zero": 0}
        overrides = {"planner": "", "worker": "Choice", "reviewer": "profile-choice"}
        body = {"requirement": " Build a calculator ", **choice, "config": config, "goal_id": "calculator",
                "profile_overrides": overrides}
        body.update({"workspace_mode": mode} if mode else {"base": "HEAD"})
        async with _serving(tmp_path) as server:
            server._client_ops = ClientOps(SimpleNamespace())
            code, answer = await post(server, body)
            assert code == 200, answer  # operator high executes in this call
            result = answer["result"]
            api_v1.validate_arguments(api_v1.find("POST", PATH).result, result)
            row = await tasks.get_task(result["task_id"])
            assert row["config"] == config and row["goal_id"] == "calculator"
            assert row["requirement"] == "Build a calculator"
            assert (row["workspace_dir"], row["workspace_mode"], row["workspace_base"]) == (
                work, mode or "worktree", "" if mode else "HEAD")
            assert result["workspace"]["mode"] == (mode or "worktree")
            assert {role: profile["id"] for role, profile in
                    (await agent_setting_layers.profile_overrides(row["id"])).items()} == {
                        "worker": "profile-choice", "reviewer": "profile-choice"}
            assert row["client_id"] is None and row["manager_id"] == "manager"
            if new_project:
                saved = await projects.get_project(result["project_id"])
                assert [saved[key] for key in ("description", "background", "task_budget_usd", "task_workspaces")] == [
                    "Description", "Background", 12, "worktree"]
    run_db(test, monkeypatch, timeout=30)


@pytest.mark.parametrize("invalid", [
    {"project": None}, {"project": {"name": "Example"}, "project_id": "other"},
    {"requirement": " "}, {"requirement": 7}, {"requirement": "x" * 32_001},
    {"config": "{}"}, {"config": []}, {"config": None},
    {"profile_overrides": {"manager": "choice"}}, {"profile_overrides": {"worker": None}},
    {"profile_overrides": {"worker": "missing"}}, {"project": {"name": "Example", "task_budget_usd": -1}},
    {"client_id": "forged"}, {"manager_id": "forged"}, {"principal": "operator"},
], ids=["missing-project", "both-projects", "blank", "type", "cap", "config-string", "config-array", "config-null",
        "unknown-role", "null-profile", "unknown-profile", "execution", "client-id", "manager-id", "principal"])
def test_invalid_requests_leave_no_project_or_task_rows(monkeypatch, tmp_path, invalid):
    async def test(pool):
        _, token = await api_clients.create("Client", pool=pool)
        body = {"requirement": "Build", "project": {"name": "Example", "work_dir": str(tmp_path)}, **invalid}
        if invalid == {"project": None}:
            body.pop("project")
        async with _serving(tmp_path) as server:
            server._client_ops = ClientOps(SimpleNamespace())
            code, answer = await post(server, body, token=token)
            assert code == 400 and answer["error"]["code"] == "invalid", answer
            assert await pool.fetchval("SELECT count(*) FROM tasks") == 0
            assert await pool.fetchval("SELECT count(*) FROM projects") == 0
    run_db(test, monkeypatch, timeout=30)


def test_new_project_waits_for_matching_client_and_session_and_creates_once(monkeypatch, tmp_path):
    async def test(pool):
        client, token = await api_clients.create("Client", pool=pool)
        _, other = await api_clients.create("Other", pool=pool)
        body = {"requirement": "Build", "project": {"name": "Example", "work_dir": str(tmp_path)}}
        async with _serving(tmp_path) as server:
            server._client_ops = ClientOps(SimpleNamespace())
            code, asked = await post(server, body, token=token)
            assert code == 202 and asked["impact"]
            confirm = {"confirmation_id": asked["confirmation_id"]}
            for identity, session in ((token, "other-session"), (other, "create")):
                code, answer = await post(server, confirm, token=identity, session=session, path="/api/v1/confirm")
                assert code == 404 and answer["error"]["code"] == "not_found"
            assert await pool.fetchval("SELECT count(*) FROM projects") == 0
            assert await pool.fetchval("SELECT count(*) FROM tasks") == 0
            code, done = await post(server, confirm, token=token, path="/api/v1/confirm")
            assert code == 200, done
            row = await tasks.get_task(done["result"]["task_id"])
            assert row["client_id"] == client["id"] and row["manager_id"] == "manager"
            assert (await post(server, confirm, token=token, path="/api/v1/confirm"))[0] == 404
            assert await pool.fetchval("SELECT count(*) FROM tasks") == 1
            assert await pool.fetchval("SELECT count(*) FROM projects") == 1
    run_db(test, monkeypatch, timeout=30)


def test_abandoned_confirmation_has_no_phantom_rows_and_is_replaced(monkeypatch, tmp_path):
    async def test(pool):
        _, token = await api_clients.create("Client", pool=pool)
        async with _serving(tmp_path) as server:
            server._client_ops = ClientOps(SimpleNamespace())
            body = {"requirement": "Build", "project": {"name": "Cancelled", "work_dir": str(tmp_path)}}
            code, abandoned = await post(server, body, token=token)
            assert code == 202
            body["project"]["name"] = "Replacement"
            code, pending = await post(server, body, token=token)
            assert code == 202 and pending["confirmation_id"] != abandoned["confirmation_id"]
            assert (await post(server, {"confirmation_id": abandoned["confirmation_id"]},
                               token=token, path="/api/v1/confirm"))[0] == 404
            assert await pool.fetchval("SELECT count(*) FROM tasks") == 0
            assert await pool.fetchval("SELECT count(*) FROM projects") == 0
    run_db(test, monkeypatch, timeout=30)


def test_authenticated_legacy_route_is_gone_with_successful_v1_control(monkeypatch, tmp_path):
    async def test(pool):
        async with _serving(tmp_path) as server:
            server._client_ops = ClientOps(SimpleNamespace())
            project = await projects.create_project(name="Example", work_dir=str(tmp_path), pool=pool)
            body = {"requirement": "Build", "project_id": project["id"]}
            assert (await post(server, body))[0] == 200
            assert (await post(server, body, path="/api/tasks/create"))[0] == 404
    run_db(test, monkeypatch, timeout=30)
