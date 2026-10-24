"""Authenticated project role operations, atomic validation and copied versions on PostgreSQL."""

import json
from contextlib import asynccontextmanager
from datetime import datetime

import pytest

from looper.agents import api_v1
from looper.agents.bus import MessageBus
from looper.agents.ops.clients import ClientOps
from looper.agents.ops.service import Platform
from looper.db import api_clients, library, projects, role_templates, task_roles
from tests.db_support import DSN, run_db
from tests.test_api_v1_auth import _bearer, _send, _serving
from tests.test_operator_auth import TOKEN
from tests.test_ops_tasks_db import _task
from tests.test_planner_tools_db import _Planner, _ops, _steps
from tests.test_task_agents_db import _profiles

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")
TEMPLATE = {"key": "security", "kind": "review", "description": "Check authentication",
            "role_prompt": "Check authentication and report findings.\nRead the tests.",
            "harness": "## How you work\nRead the code first.", "profile_id": None, "items": []}


@asynccontextmanager
async def _api(tmp_path, pool):
    _, token = await api_clients.create("role template validation", pool=pool)
    async with _serving(tmp_path) as server:
        server._client_ops = ClientOps(Platform(bus=MessageBus(runtime_root=tmp_path),
                                               task_router=server._task_router, runtime_root=tmp_path))

        async def call(action, body=None, *, client=False, method="POST", session="role-templates"):
            path = action if action.startswith("/") else "/api/v1/" + action.replace(".", "/")
            status, _, answer = await _send(server, f"{method} {path}",
                {**_bearer(token if client else TOKEN), "X-Looper-Session": session}, json.dumps(body or {}))
            if status == 200 and not action.startswith("/") and action != "confirm":
                api_v1.validate_arguments({"type": "object", "properties": {
                    "result": api_v1.find(method, path).result}}, {"result": answer["result"]})
            return status, answer
        yield call


async def _seed(pool):
    project = await projects.create_project(name="Example", execution={"role_text_max_chars": 500}, pool=pool)
    await _profiles(pool)
    await projects.set_allowed_profiles(project["id"], ["agent-sonnet"], pool=pool)
    for item_id, name in (("item-notes", "notes"), ("item-lint", "lint"), ("item-uninstalled", "uninstalled")):
        await pool.execute("INSERT INTO library_items (id, kind, name, sha256, size_bytes, file_count, description) "
                           "VALUES ($1, 'skill', $2, 'example', 1, 1, 'Useful instructions')", item_id, name)
        if item_id != "item-uninstalled":
            await library.insert_install(pool, item_id=item_id, scope="project", project_id=project["id"],
                                         roles=["task_role"], enabled=True)
    return project


@pytest.mark.parametrize("client", [False, True], ids=["operator", "client"])
def test_template_create_replace_and_list_read_exact_content_profile_and_ordered_items(tmp_path, monkeypatch, client):
    async def run(pool):
        project = await _seed(pool)
        body = {**TEMPLATE, "project_id": project["id"], "profile_id": "agent-sonnet",
                "items": ["item-notes", "item-lint"]}
        async with _api(tmp_path, pool) as call:
            status, added = await call("role_templates.put", body, client=client)
            assert status == 200, added
            result = added["result"]
            entry = result["templates"][0]
            assert {field: entry[field] for field in ("key", "kind", "description", "role_prompt", "harness")} == {
                field: body[field] for field in ("key", "kind", "description", "role_prompt", "harness")}
            assert entry["profile"] == {"profile_id": "agent-sonnet", "name": "claude-code/sonnet", "allowed": True}
            assert entry["items"] == [{"id": "item-notes", "kind": "skill", "name": "notes"},
                                      {"id": "item-lint", "kind": "skill", "name": "lint"}]
            assert result["project_id"] == project["id"] and result["role_text_max_chars"] == 500
            assert {item["id"] for item in result["role_items"]} == set(body["items"])
            assert all(item["description"] == "Useful instructions" for item in result["role_items"])
            assert entry["updated_at"]
            assert (await call("role_templates.list", {"project_id": project["id"]}, client=client))[1]["result"] == result
            replacement = {**body, "kind": "work", "role_prompt": "Implement regression tests.",
                           "description": "Changed", "harness": None, "profile_id": None, "items": []}
            status, answer = await call("role_templates.put", replacement, client=client)
            assert status == 200, answer
            entry = answer["result"]["templates"][0]
            assert entry["kind"] == "work" and entry["role_prompt"] == replacement["role_prompt"]
            assert entry["description"] == "Changed" and entry["profile"] is None and entry["harness"] is None
            assert entry["items"] == []
            row = await role_templates.get_template(project["id"], TEMPLATE["key"])
            assert row["role_prompt"] == replacement["role_prompt"] and row["items"] == []
    run_db(run, monkeypatch, timeout=30)


@pytest.mark.parametrize(("field", "value"), [
    *[("key", value) for value in ("", "-start", "has space", "a" * 33, "Worker", "reviewer", "planner", "manager")],
    ("kind", "plan"), ("description", "x" * 301), ("role_prompt", "  "),
    ("role_prompt", "x" * 501), ("harness", "x" * 501), ("profile_id", "missing-profile"),
    ("profile_id", "agent-one-off"), ("items", ["missing-item"]), ("items", ["item-uninstalled"]), ("items", [""]),
    ("items", ["item-notes", "notes"]), ("items", ["item-notes"] * 21),
    ("items", "item-notes"), ("extra", True),
])
def test_definition_rule_failure_never_partially_overwrites_template(tmp_path, monkeypatch, field, value):
    async def run(pool):
        project = await _seed(pool)
        body = {**TEMPLATE, "project_id": project["id"]}
        async with _api(tmp_path, pool) as call:
            assert (await call("role_templates.put", body))[0] == 200
            before = await role_templates.list_templates(project["id"])
            status, answer = await call("role_templates.put", {**body, "description": "Must not persist", field: value},
                                        client=True)
            assert status == 400 and answer["error"]["code"] == "invalid", answer
            assert await role_templates.list_templates(project["id"]) == before
    run_db(run, monkeypatch, timeout=30)


@pytest.mark.parametrize(("field", "value"), [("key", "A" + "a_.-" * 7 + "xyz"),
    ("description", "x" * 300), ("role_prompt", "界" * 500), ("harness", "x" * 500)])
def test_definition_accepts_exact_field_boundaries(tmp_path, monkeypatch, field, value):
    async def run(pool):
        project = await _seed(pool)
        async with _api(tmp_path, pool) as call:
            status, answer = await call("role_templates.put", {**TEMPLATE, "project_id": project["id"], field: value})
            assert status == 200 and answer["result"]["templates"][0][field] == value, answer
    run_db(run, monkeypatch, timeout=30)


@pytest.mark.parametrize("action", ["role_templates.list", "role_templates.put", "role_templates.delete", "project_roles.stats"])
def test_unknown_project_is_not_found(tmp_path, monkeypatch, action):
    async def run(pool):
        async with _api(tmp_path, pool) as call:
            body = {**(TEMPLATE if action.endswith("put") else {}), "project_id": "proj-missing"}
            if action.endswith("delete"):
                body["key"] = "security"
            status, answer = await call(action, body, client=True)
            assert status == 404 and answer["error"]["code"] == "not_found", answer
    run_db(run, monkeypatch, timeout=30)


def test_unknown_template_delete_is_not_found_without_confirmation(tmp_path, monkeypatch):
    async def run(pool):
        project = await _seed(pool)
        async with _api(tmp_path, pool) as call:
            status, answer = await call("role_templates.delete", {"project_id": project["id"], "key": "missing"}, client=True)
            assert status == 404 and answer["error"]["code"] == "not_found", answer
    run_db(run, monkeypatch, timeout=30)


@pytest.mark.parametrize("removed", ["profile", "item"])
def test_removed_reference_is_readable_but_cannot_be_saved_again(tmp_path, monkeypatch, removed):
    async def run(pool):
        project = await _seed(pool)
        body = {**TEMPLATE, "project_id": project["id"], "profile_id": "agent-sonnet", "items": ["item-notes"]}
        async with _api(tmp_path, pool) as call:
            assert (await call("role_templates.put", body))[0] == 200
            if removed == "profile":
                await projects.set_allowed_profiles(project["id"], [], pool=pool)
                await pool.execute("DELETE FROM agent_profiles WHERE id = 'agent-sonnet'")
            else:
                await library.delete_item("item-notes", pool=pool)
            before = await role_templates.list_templates(project["id"])
            status, read = await call("role_templates.list", {"project_id": project["id"]}, client=True)
            assert status == 200, read
            entry = read["result"]["templates"][0]
            if removed == "profile":
                assert entry["profile"] is None
            else:
                assert entry["items"] == [{"id": "item-notes", "kind": None, "name": None}]
            status, answer = await call("role_templates.put", {**body, "role_prompt": "Must not persist"}, client=True)
            assert status == 400 and answer["error"]["code"] == "invalid", answer
            assert await role_templates.list_templates(project["id"]) == before
    run_db(run, monkeypatch, timeout=30)


@pytest.mark.parametrize("client", [True, False], ids=["confirmed-client", "single-call-operator"])
def test_template_replace_and_delete_preserve_already_copied_task_role_versions(tmp_path, monkeypatch, client):
    async def run(pool):
        project = await _seed(pool)
        body = {**TEMPLATE, "project_id": project["id"], "profile_id": "agent-sonnet", "items": ["item-notes"]}
        async with _api(tmp_path, pool) as call:
            assert (await call("role_templates.put", body))[0] == 200
            await _task(pool, "task-copy", "running", project)
            await projects.update_project(project["id"], execution={"planner_roles": "on"}, pool=pool)
            copied = await _Planner(_ops(), "task-copy")("looper_task_role_set", {"template": body["key"]})
            assert copied["status"] == "ok" and copied["result"]["version"] == 1, copied
            before = await task_roles.versions("task-copy")
            assert before[0]["role_prompt"] == body["role_prompt"] and before[0]["items"] == body["items"]
            assert (await call("role_templates.put", {**body, "role_prompt": "Changed source"}))[0] == 200
            status, answer = await call("role_templates.delete", {"project_id": project["id"], "key": body["key"]}, client=client)
            if client:
                assert status == 202 and answer["status"] == "needs_confirmation", answer
                assert body["key"] in answer["impact"] and project["id"] in answer["impact"]
                assert "versions" in answer["impact"] and "copied" in answer["impact"]
                assert await role_templates.get_template(project["id"], body["key"])
                confirm = {"confirmation_id": answer["confirmation_id"]}
                assert (await call("confirm", confirm, client=True, session="other"))[0] == 404
                status, answer = await call("confirm", confirm, client=True)
            assert status == 200 and answer["result"]["templates"] == [], answer
            assert await role_templates.get_template(project["id"], body["key"]) is None
            assert await task_roles.versions("task-copy") == before
    run_db(run, monkeypatch, timeout=30)


def test_nonempty_stats_filter_task_creation_date_and_preserve_zero_denominator_nulls(tmp_path, monkeypatch):
    async def run(pool):
        project = await _seed(pool)
        for index, created in enumerate(("2020-01-01", "2020-01-03")):
            task_id = f"task-{index}"
            await _task(pool, task_id, "running", project)
            await pool.execute("UPDATE tasks SET created_at = $2 WHERE id = $1", task_id,
                               datetime.fromisoformat(created + "T12:00:00+00:00"))
            await task_roles.add_version(task_id, "security", {**TEMPLATE, "template_key": "security"},
                {"step_id": "", "kind": "role_defined", "summary": "Defined role", "learnings": []}, after=0)
        await _steps(pool, "task-1", fresh="verified")
        await pool.execute("UPDATE task_steps SET review = true, review_roles = "
                           "'[{\"role\":\"security\",\"required\":true,\"version\":1}]' WHERE id = 'step-fresh'")
        await pool.execute("INSERT INTO llm_records (agent_id, task_id, status, cost_usd, metadata) "
            "VALUES ('reviewer-one', 'task-1', 'completed', 0.25, $1::jsonb)", json.dumps({"step_id": "step-fresh",
            "outcome": "passed", "prompt_layers": {"task_role": {"id": "security", "kind": "review", "version": 1}}}))
        async with _api(tmp_path, pool) as call:
            status, answer = await call("project_roles.stats", {"project_id": project["id"]}, client=True)
            assert status == 200 and answer["result"]["tasks"] == 2, answer
            role = answer["result"]["roles"][0]
            assert role["id"] == "security" and role["tasks"] == 2 and role["templates"] == ["security"]
            assert role["kinds"] == ["review"]
            assert role["stats"]["runs"] == 1 and role["stats"]["cost_usd"] == 0.25
            assert role["stats"]["review_pass_rate"] == 1 and role["stats"]["contract_violation_rate"] == 0
            assert role["stats"]["steps"] == role["stats"]["accepted"] == 1
            for stats in answer["result"]["builtin"].values():
                assert stats["contract_violation_rate"] is None and stats["review_pass_rate"] is None
            for since, expected in (("2020-01-02", 1), ("2020-01-03", 1), ("2020-01-04", 0)):
                status, filtered = await call("project_roles.stats", {"project_id": project["id"], "since": since})
                assert status == 200 and filtered["result"]["tasks"] == expected, filtered
                json.dumps(filtered["result"], allow_nan=False)
            status, answer = await call("project_roles.stats", {"project_id": project["id"], "since": "2020-02-30"})
            assert status == 400 and answer["error"]["code"] == "invalid", answer
    run_db(run, monkeypatch, timeout=30)


@pytest.mark.parametrize(("method", "suffix", "action"), [("GET", "role-stats", "project_roles.stats"),
    ("GET", "role-templates", "role_templates.list"), ("PUT", "role-templates/security", "role_templates.put"),
    ("DELETE", "role-templates/security", "role_templates.delete")])
def test_retired_routes_return_404_with_authenticated_same_server_v1_positive_control(tmp_path, monkeypatch, method, suffix, action):
    async def run(pool):
        project = await _seed(pool)
        async with _api(tmp_path, pool) as call:
            body = {**TEMPLATE, "project_id": project["id"]}
            assert (await call("role_templates.put", body))[0] == 200
            assert (await call(f"/api/projects/{project['id']}/{suffix}", method=method))[0] == 404
            arguments = body if method == "PUT" else {"project_id": project["id"]}
            if method == "DELETE":
                arguments["key"] = "security"
            status, answer = await call(action, arguments)
            assert status == 200 and answer["status"] == "ok", answer
    run_db(run, monkeypatch, timeout=30)
