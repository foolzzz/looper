"""Authenticated full/compact profiles, compatibility and deletion rechecks on PostgreSQL."""

import json
from contextlib import asynccontextmanager

import pytest

from looper.agents import api_v1, profile_registry, runtimes
from looper.agents.bus import MessageBus
from looper.agents.ops.clients import ClientOps
from looper.agents.ops.service import Platform
from looper.db import agent_setting_layers, agent_sessions, api_clients, profile_registry as db, projects, tasks
from tests.db_support import DSN, run_with_models
from tests.test_api_v1_auth import _bearer, _send, _serving
from tests.test_operator_auth import TOKEN

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")


@asynccontextmanager
async def _api(tmp_path, pool):
    _, token = await api_clients.create("Profile validation", pool=pool)
    async with _serving(tmp_path) as server:
        server._client_ops = ClientOps(Platform(bus=MessageBus(runtime_root=tmp_path),
            task_router=server._task_router, runtime_root=tmp_path))

        async def call(operation, body=None, *, client=False, method="POST"):
            prefix = "/api/v1/ops/" if operation.startswith("agent_profiles.") else "/api/v1/"
            path = operation if operation.startswith("/") else prefix + operation.replace(".", "/")
            status, _, answer = await _send(server, f"{method} {path}",
                {**_bearer(token if client else TOKEN), "X-Looper-Session": "profiles"}, json.dumps(body or {}))
            if status == 200 and not operation.startswith("/") and operation != "confirm":
                api_v1.validate_arguments({"type": "object", "properties": {
                    "result": api_v1.find(method, path).result}}, {"result": answer["result"]})
            return status, answer
        yield call


async def _create(call, **fields):
    model = (await profile_registry.create_model({"provider_id": "prov-test", "name": "example-model",
                                                 "protocol": "anthropic-messages"}))["model"]
    body = {"name": "Example profile", "runtime_id": "rt-claude-code", "model_id": model["model_id"], **fields}
    status, answer = await call("profiles.create", body)
    assert status == 200 and answer["result"]["profile"], answer
    return answer["result"]["profile"]


def test_full_and_compact_profiles_preserve_usage_shapes_and_warning_results(tmp_path, monkeypatch):
    async def run(pool):
        async with _api(tmp_path, pool) as call:
            profile = await _create(call, effort="high", custom_prompt="Profile identity")
            pid = profile["profile_id"]
            status, created = await call("agent_profiles.create", {"name": "Compact", "runtime": "rt-claude-code",
                                                                  "model": profile["model_id"]})
            assert status == 200 and created["result"]["profile"]["used_by"] is None
            assert isinstance(created["result"]["warnings"], list)
            await agent_sessions.select_profile("manager", pid)
            status, full = await call("profiles.update", {"profile_id": pid, "fields": {"effort": None}}, client=True)
            assert status == 200 and full["result"]["warnings"] == full["result"]["profile"]["warnings"]
            full = full["result"]["profile"]
            assert full["used_by"]["manager_sessions"] == ["manager"] and full["custom_prompt"] == "Profile identity"
            status, compact = await call("agent_profiles.update", {"profile": pid, "name": "Renamed"})
            assert status == 200 and isinstance(compact["result"]["warnings"], list)
            compact = compact["result"]["profile"]
            assert isinstance(compact["used_by"], str) and "session" in compact["used_by"]
            assert set(compact) == {"profile_id", "name", "runtime", "provider", "model_id", "model", "effort",
                                    "agent_teams", "custom_prompt", "roles", "builtin", "used_by"}
            listing = (await call("agent_profiles.list"))[1]["result"]["profiles"]
            assert next(p for p in listing if p["profile_id"] == pid) == compact
            assert (await db.get_profile(pid))["name"] == "Renamed" and full["effort"] is None
            await agent_sessions.select_profile("manager", None)
            status, deleted = await call("agent_profiles.delete", {"profile": pid})
            assert status == 200 and deleted["result"] == {"deleted": "Renamed"} and await db.get_profile(pid) is None
    run_with_models(run, monkeypatch)


@pytest.mark.parametrize("action", ["create", "update"])
@pytest.mark.parametrize("changes,valid", [({"effort": "high"}, True), ({"effort": None}, True),
    ({"effort": "unsupported"}, False), ({"runtime_id": "missing-runtime"}, False),
    ({"model_id": "missing-model"}, False), ({"model_id": "incompatible"}, False)])
def test_profile_create_update_runtime_model_compatibility_is_atomic(tmp_path, monkeypatch, action, changes, valid):
    async def run(pool):
        async with _api(tmp_path, pool) as call:
            profile = await _create(call)
            incompatible = (await profile_registry.create_model({"provider_id": "prov-test", "name": "chat-model",
                                                                  "protocol": "openai-chat"}))["model"]
            fields = {k: incompatible["model_id"] if v == "incompatible" else v for k, v in changes.items()}
            body = ({"name": "Another", "runtime_id": profile["runtime_id"], "model_id": profile["model_id"], **fields}
                    if action == "create" else {"profile_id": profile["profile_id"], "fields": fields})
            before = await pool.fetch("SELECT * FROM agent_profiles ORDER BY id")
            status, answer = await call("profiles." + action, body, client=True)
            assert status == (200 if valid else 400), answer
            if valid:
                assert answer["result"]["profile"]["effort"] == fields["effort"]
            else:
                assert answer["error"]["code"] == "invalid"
                assert await pool.fetch("SELECT * FROM agent_profiles ORDER BY id") == before
    run_with_models(run, monkeypatch)


@pytest.mark.parametrize("role,valid", [("worker", True), ("planner", False), ("manager", False)])
def test_profile_update_preserves_selected_role_capabilities(tmp_path, monkeypatch, role, valid):
    class OneShot(type(runtimes.CLAUDE_CODE)):
        type = "profile-one-shot"
        capabilities = frozenset({runtimes.Capability.RUN})

    monkeypatch.setitem(runtimes.ADAPTERS, OneShot.type, OneShot())

    async def run(pool):
        runtime_id = "rt-profile-one-shot"
        await pool.execute("INSERT INTO agent_runtimes (id,name,type,executable,capabilities,builtin) "
                           "VALUES ($1,'One shot',$2,'',ARRAY['run'],true)", runtime_id, OneShot.type)
        async with _api(tmp_path, pool) as call:
            pid = (await _create(call))["profile_id"]
            await agent_setting_layers.put_layer("role", role, None, {"profile_id": pid})
            status, answer = await call("profiles.update", {"profile_id": pid,
                "fields": {"runtime_id": runtime_id, "agent_teams": False}})
            assert status == (200 if valid else 400), answer
            assert (await db.get_profile(pid))["runtime_id"] == (runtime_id if valid else "rt-claude-code")
    run_with_models(run, monkeypatch)


async def _reference(pool, pid, kind):
    if kind == "session":
        await agent_sessions.select_profile("manager", pid)
    elif kind in {"manager", "template"}:
        await pool.execute("INSERT INTO managers (id,name,system_prompt,profile_id) VALUES "
            "('manager-example','Example','Reply briefly.',$1)", pid if kind == "manager" else None)
        if kind == "template":
            await pool.execute("INSERT INTO manager_task_templates (manager_id,key,name,worker_profile_id) "
                               "VALUES ('manager-example','check','Check',$1)", pid)
    else:
        project = await projects.create_project(name="Example")
        await tasks.create_task(task_id="task-example", requirement="Check", project_id=project["id"], status=tasks.READY)
        table = "task_profile_overrides" if kind == "task" else "task_planner_profiles"
        await pool.execute(f"INSERT INTO {table} (task_id,role,profile_id) VALUES ('task-example','worker',$1)", pid)


@pytest.mark.parametrize("kind", ["builtin", "session", "manager", "template", "task", "planner-choice"])
def test_builtin_and_every_live_reference_block_profile_deletion(tmp_path, monkeypatch, kind):
    async def run(pool):
        async with _api(tmp_path, pool) as call:
            pid = (await _create(call))["profile_id"]
            if kind == "builtin":
                pid = await pool.fetchval("SELECT id FROM agent_profiles WHERE builtin LIMIT 1")
            else:
                await _reference(pool, pid, kind)
            status, answer = await call("profiles.delete", {"profile_id": pid}, client=kind != "planner-choice")
            assert status == (400 if kind == "builtin" else 409), answer
            assert answer["error"]["code"] == ("invalid" if kind == "builtin" else "conflict")
            assert await db.get_profile(pid) is not None and "confirmation_id" not in answer
    run_with_models(run, monkeypatch)


@pytest.mark.parametrize("new_reference", [None, "session", "manager", "template", "task", "planner-choice"])
def test_client_delete_has_no_mutation_before_confirmation_and_rechecks_references(tmp_path, monkeypatch, new_reference):
    async def run(pool):
        async with _api(tmp_path, pool) as call:
            pid = (await _create(call))["profile_id"]
            status, pending = await call("profiles.delete", {"profile_id": pid}, client=True)
            assert status == 202 and pending["status"] == "needs_confirmation" and pending["impact"]
            assert await db.get_profile(pid) is not None
            if new_reference:
                await _reference(pool, pid, new_reference)
            status, answer = await call("confirm", {"confirmation_id": pending["confirmation_id"]}, client=True)
            assert status == (409 if new_reference else 200), answer
            if new_reference:
                assert answer["error"]["code"] == "conflict" and await db.get_profile(pid) is not None
            else:
                assert answer["result"] == {"deleted": pid} and await db.get_profile(pid) is None
    run_with_models(run, monkeypatch)


@pytest.mark.parametrize("field,value,status,code", [("project_id", "missing-project", 404, "not_found"),
                                                    ("role", "unknown-role", 400, "invalid")])
def test_preview_errors_are_typed_and_successful_preview_does_not_write(tmp_path, monkeypatch, field, value, status, code):
    async def run(pool):
        async with _api(tmp_path, pool) as call:
            pid = (await _create(call))["profile_id"]
            before = await pool.fetchrow("SELECT (SELECT count(*) FROM manager_actions) AS actions, "
                                        "(SELECT count(*) FROM agent_sessions) AS sessions")
            body = {"profile_id": pid, "role": "worker"}
            ok, preview = await call("profiles.prompt_preview", body)
            assert ok == 200 and preview["result"]["blocks"] and preview["result"]["looper_prompt_sha256"]
            failed, answer = await call("profiles.prompt_preview", {**body, field: value}, client=True)
            assert failed == status and answer["error"]["code"] == code, answer
            assert await pool.fetchrow("SELECT (SELECT count(*) FROM manager_actions) AS actions, "
                                       "(SELECT count(*) FROM agent_sessions) AS sessions") == before
    run_with_models(run, monkeypatch)


@pytest.mark.parametrize("action", ["update", "delete", "prompt_preview"])
def test_unknown_profile_is_typed_not_found_after_successful_control(tmp_path, monkeypatch, action):
    async def run(pool):
        async with _api(tmp_path, pool) as call:
            pid = (await _create(call))["profile_id"]
            extra = {"fields": {"name": "Changed"}} if action == "update" else {"role": "worker"} if action == "prompt_preview" else {}
            assert (await call("profiles." + action, {"profile_id": pid, **extra}))[0] == 200
            status, answer = await call("profiles." + action, {"profile_id": "missing-profile", **extra})
            assert status == 404 and answer["error"]["code"] == "not_found", answer
    run_with_models(run, monkeypatch)


@pytest.mark.parametrize("method,action", [("POST", "create"), ("PUT", "update"),
                                           ("DELETE", "delete"), ("GET", "prompt_preview")])
def test_retired_profile_routes_have_authenticated_v1_positive_controls(tmp_path, monkeypatch, method, action):
    async def run(pool):
        async with _api(tmp_path, pool) as call:
            profile = await _create(call)
            pid = profile["profile_id"]
            path = "/api/agent-profiles" + ("/" + pid if action != "create" else "")
            path += "/prompt-preview?role=worker" if action == "prompt_preview" else ""
            body = {"profile_id": pid}
            if action == "create":
                body = {"name": "Another", "runtime_id": profile["runtime_id"], "model_id": profile["model_id"]}
            elif action == "update":
                body["fields"] = {"name": "Changed"}
            elif action == "prompt_preview":
                body["role"] = "worker"
            assert (await call(path, body, method=method))[0] == 404
            status, answer = await call("profiles." + action, body)
            assert status == 200 and answer["result"], answer
            if action == "delete":
                assert await db.get_profile(pid) is None
    run_with_models(run, monkeypatch)
