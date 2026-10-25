"""Authenticated Manager writes, confirmation rechecks and retired HTTP routes."""

import json
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from looper.agents import api_v1, managers_api
from looper.agents.ops.clients import ClientOps
from looper.db import api_clients, projects, tasks
from tests.db_support import DSN, run_db
from tests.test_api_v1_auth import _bearer, _send, _serving
from tests.test_managers_api_db import _body, _profile, _template
from tests.test_operator_auth import TOKEN
from tests.test_task_agents_db import _profiles

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")


@asynccontextmanager
async def _api(tmp_path, pool):
    _, token = await api_clients.create("Manager validation", pool=pool)
    async with _serving(tmp_path) as server:
        server._client_ops = ClientOps(SimpleNamespace())
        server._managers = SimpleNamespace(state=lambda _: {
            "running": False, "busy": False, "healthy": False, "sync": "pending", "sync_error": "retrying"})
        async def call(operation, body=None, *, client=False, method="POST"):
            path = operation if operation.startswith("/") else "/api/v1/" + operation.replace(".", "/")
            status, _, answer = await _send(server, f"{method} {path}",
                {**_bearer(token if client else TOKEN), "X-Looper-Session": "manager-validation"}, json.dumps(body or {}))
            if status == 200 and not operation.startswith("/") and operation != "confirm":
                api_v1.validate_arguments(api_v1.find(method, path).result, answer["result"])
            return status, answer
        yield call


@pytest.mark.parametrize("operation,body,method,path", [
    ("managers.list", {}, "GET", "/api/managers"),
    ("managers.create", _body("new"), "POST", "/api/managers"),
    ("managers.update", {"manager_id": "manager-review", "fields": {"description": "Updated"}},
     "PUT", "/api/managers/manager-review"),
    ("managers.delete", {"manager_id": "manager-idle"}, "DELETE", "/api/managers/manager-review"),
    ("manager_templates.put", {"manager_id": "manager-review", "key": "pr", **_template()},
     "PUT", "/api/managers/manager-review/templates/pr"),
    ("manager_templates.delete", {"manager_id": "manager-review", "key": "other"},
     "DELETE", "/api/managers/manager-review/templates/pr"),
])
def test_retired_routes_have_authenticated_v1_positive_controls(tmp_path, monkeypatch, operation, body, method, path):
    async def run(pool):
        await managers_api.create_manager(_body("review"))
        await managers_api.create_manager(_body("idle"))
        await managers_api.put_template("manager-review", "pr", _template())
        await managers_api.put_template("manager-review", "other", _template())
        async with _api(tmp_path, pool) as call:
            status, answer = await call(operation, body)
            assert status == 200, answer
            assert (await call(path, body, method=method))[0] == 404
    run_db(run, monkeypatch, timeout=30)


def test_list_includes_runtime_pending_error_and_spend(tmp_path, monkeypatch):
    async def run(pool):
        await managers_api.create_manager(_body("review"))
        await pool.execute("INSERT INTO llm_records (agent_id, status, cost_usd) VALUES ('manager-review', 'completed', 2.5)")
        async with _api(tmp_path, pool) as call:
            status, answer = await call("managers.list", client=True)
            assert status == 200
            manager = next(m for m in answer["result"]["managers"] if m["id"] == "manager-review")
            assert manager["state"]["sync"] == "pending" and manager["state"]["sync_error"] == "retrying"
            assert manager["spend"] == {"today_usd": 2.5, "last_7d_usd": 2.5, "runs_today": 1}
    run_db(run, monkeypatch, timeout=30)


@pytest.mark.parametrize("operation,body", [
    ("managers.create", _body("new", enabled=False)),
    ("managers.update", {"manager_id": "manager-review", "fields": {"tools": "all", "task_scope": "all"}}),
    ("managers.delete", {"manager_id": "manager-review"}),
    ("manager_templates.put", {"manager_id": "manager-review", "key": "pr", **_template(name="Changed")}),
    ("manager_templates.delete", {"manager_id": "manager-review", "key": "pr"}),
])
def test_client_definition_writes_wait_for_explicit_confirmation(tmp_path, monkeypatch, operation, body):
    async def run(pool):
        await managers_api.create_manager(_body("review", enabled=False))
        await managers_api.put_template("manager-review", "pr", _template())
        before = await managers_api.managers_view()
        async with _api(tmp_path, pool) as call:
            status, asked = await call(operation, body, client=True)
            assert status == 202 and asked["status"] == "needs_confirmation" and asked["impact"]
            assert await managers_api.managers_view() == before
            status, answer = await call("confirm", {"confirmation_id": asked["confirmation_id"]}, client=True)
            assert status == 200, answer
            api_v1.validate_arguments(api_v1.find("POST", "/api/v1/" + operation.replace(".", "/")).result,
                                      answer["result"])
            assert await managers_api.managers_view() != before
    run_db(run, monkeypatch, timeout=30)


@pytest.mark.parametrize("reference", ["project", "profile", "role_template"])
def test_pending_template_does_not_write_and_confirm_rechecks_references(tmp_path, monkeypatch, reference):
    async def run(pool):
        await _profiles(pool)
        project = await projects.create_project(name="Example")
        profile = await _profile(pool)
        await pool.execute("INSERT INTO project_role_templates (project_id, template_key, kind, role_prompt) "
                           "VALUES ($1, 'security', 'review', 'Check.')", project["id"])
        await managers_api.create_manager(_body("review"))
        body = {"manager_id": "manager-review", "key": "pr", **_template(
            project_id=project["id"], profiles={"reviewer": profile}, role_templates=["security"])}
        before = await managers_api.managers_view()
        async with _api(tmp_path, pool) as call:
            status, asked = await call("manager_templates.put", body, client=True)
            assert status == 202 and asked["status"] == "needs_confirmation"
            assert "manager-review" in asked["impact"] and "pr" in asked["impact"]
            assert await managers_api.managers_view() == before
            if reference == "project":
                await pool.execute("DELETE FROM projects WHERE id = $1", project["id"])
            elif reference == "profile":
                await pool.execute("DELETE FROM agent_profiles WHERE id = $1", profile)
            else:
                await pool.execute("DELETE FROM project_role_templates WHERE project_id = $1", project["id"])
            status, answer = await call("confirm", {"confirmation_id": asked["confirmation_id"]}, client=True)
            assert status == 400 and answer["error"]["code"] == "invalid"
            assert await managers_api.managers_view() == before
    run_db(run, monkeypatch, timeout=30)


@pytest.mark.parametrize("input_fields", [{}, {"inputs": []}], ids=["omitted", "empty"])
@pytest.mark.parametrize("replace", [False, True], ids=["create", "replace"])
def test_no_input_template_save_and_readback(tmp_path, monkeypatch, input_fields, replace):
    async def run(pool):
        await managers_api.create_manager(_body("review"))
        async with _api(tmp_path, pool) as call:
            if replace:
                status, answer = await call("manager_templates.put", {
                    "manager_id": "manager-review", "key": "pr", **_template()})
                assert status == 200, answer
                old = await pool.fetchval(
                    "SELECT inputs FROM manager_task_templates WHERE manager_id = 'manager-review' AND key = 'pr'")
                assert json.loads(old)[0]["key"] == "pr_url"
            body = {"manager_id": "manager-review", "key": "pr", "name": "Repository review",
                    "requirement": "Review the repository.", **input_fields}
            status, answer = await call("manager_templates.put", body)
            assert status == 200, answer
            saved_manager = next(m for m in answer["result"]["managers"] if m["id"] == "manager-review")
            (saved_template,) = saved_manager["templates"]
            assert saved_template["inputs"] == []
            status, listed = await call("managers.list")
            assert status == 200, listed
            manager = next(m for m in listed["result"]["managers"] if m["id"] == "manager-review")
            (template,) = manager["templates"]
            assert template["key"] == "pr" and template["name"] == body["name"]
            assert template["inputs"] == [] and template["requirement"] == body["requirement"]
            stored = await pool.fetchrow(
                "SELECT inputs, requirement FROM manager_task_templates "
                "WHERE manager_id = 'manager-review' AND key = 'pr'")
            assert json.loads(stored["inputs"]) == [] and stored["requirement"] == body["requirement"]
    run_db(run, monkeypatch, timeout=30)


def test_omitted_inputs_still_refuses_unresolved_placeholder(tmp_path, monkeypatch):
    async def run(pool):
        await managers_api.create_manager(_body("review"))
        async with _api(tmp_path, pool) as call:
            body = {"manager_id": "manager-review", "key": "pr", **_template()}
            status, answer = await call("manager_templates.put", body)
            assert status == 200, answer
            before = await managers_api.managers_view()
            del body["inputs"]
            status, answer = await call("manager_templates.put", body)
            assert status == 400 and "names no input" in answer["error"]["message"], answer
            assert await managers_api.managers_view() == before
    run_db(run, monkeypatch, timeout=30)


@pytest.mark.parametrize("fields,message", [
    ({"inputs": None}, "declared schema"),
    ({"inputs": ""}, "declared schema"),
    ({"inputs": {}}, "declared schema"),
    ({"inputs": False}, "declared schema"),
    ({"inputs": 1}, "declared schema"),
    ({"inputs": ["pr_url"]}, "declared schema"),
    ({"inputs": [{}]}, "declared schema"),
    ({"inputs": [{"key": "pr_url"}]}, "declared schema"),
    ({"inputs": [{"key": "pr_url", "label": ""}]}, "label"),
    ({"color": 1}, "declared schema"), ({"key": "Bad"}, "key"),
    ({"requirement": "Review {{nope}}."}, "names no input"),
    ({"inputs": [{"key": "pr_url", "label": "Link", "pattern": "("}]}, "does not compile"),
    ({"inputs": [{"key": "pr_url", "label": "Link", "pattern": "(a+)+$"}]}, "too slow"),
    ({"inputs": [{"key": "Bad", "label": "Link"}]}, "key"),
    ({"inputs": [{"key": "pr_url", "label": "Link", "kind": "file"}]}, "declared schema"),
    ({"role_templates": ["security"]}, "need the template's project"),
    ({"project_id": "selected-project", "role_templates": ["security"]}, "has no role template 'security'"),
    ({"project_id": "selected-project", "role_templates": ["security", "security"]}, "security twice"),
    ({"work_dir": "rel/dir"}, "absolute"), ({"project_id": "proj-nope"}, "no project 'proj-nope'"),
    ({"profiles": {"planner": "one-off/opus"}}, "profiles.planner"),
    ({"profiles": {"reviewer": "nope"}}, "no Agent Profile"),
    ({"profiles": {"manager": "claude-code/sonnet"}}, "declared schema"),
])
def test_template_validation_preserves_the_existing_definition(tmp_path, monkeypatch, fields, message):
    async def run(pool):
        await _profiles(pool)
        project = await projects.create_project(name="Example")
        await managers_api.create_manager(_body("review"))
        async with _api(tmp_path, pool) as call:
            body = {"manager_id": "manager-review", "key": "pr", **_template()}
            status, saved = await call("manager_templates.put", body)
            assert status == 200, saved
            before = await managers_api.managers_view()
            invalid = {**body, **fields}
            if "inputs" in fields:
                invalid["requirement"] = "Review the repository."
            if invalid.get("project_id") == "selected-project":
                invalid["project_id"] = project["id"]
            status, answer = await call("manager_templates.put", invalid)
            assert status == 400 and message in answer["error"]["message"], answer
            assert await managers_api.managers_view() == before
    run_db(run, monkeypatch, timeout=30)


def test_deletes_preserve_task_template_snapshot_and_thread_owner(tmp_path, monkeypatch):
    async def run(pool):
        project = await projects.create_project(name="Example")
        await managers_api.create_manager(_body("review"))
        await managers_api.create_manager(_body("idle"))
        await managers_api.put_template("manager-review", "pr", _template())
        snapshot = {"key": "pr", "inputs": {"pr_url": "https://example.com/review"}}
        await tasks.create_task(task_id="task-review", requirement="Review", project_id=project["id"],
                                manager_id="manager-review", template=snapshot)
        await pool.execute("INSERT INTO chat_threads (session_id, manager_id) VALUES ('thread-idle', 'manager-idle')")
        async with _api(tmp_path, pool) as call:
            assert (await call("manager_templates.delete", {"manager_id": "manager-review", "key": "pr"}))[0] == 200
            assert (await call("managers.delete", {"manager_id": "manager-review"}))[0] == 409
            assert (await call("managers.delete", {"manager_id": "manager-idle"}))[0] == 200
        task = await tasks.get_task("task-review")
        assert task["manager_id"] == "manager-review" and task["template"] == snapshot
        assert await pool.fetchval("SELECT manager_id FROM chat_threads WHERE session_id = 'thread-idle'") == "manager-idle"
    run_db(run, monkeypatch, timeout=30)
