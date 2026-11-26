"""Operator profile contracts through authenticated HTTP and disposable PostgreSQL."""

from unittest.mock import AsyncMock

import pytest

from looper.agents import agent_settings, api_v1, profile_registry, task_conversations_settings
from looper.agents import task_manager_settings as settings
from looper.agents.tiers import task_manager
from looper.db import agent_setting_layers, agent_sessions, profile_registry as registry, projects, tasks
from tests.db_support import run_db, v1_api


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    monkeypatch.setattr(task_conversations_settings, "_enabled", True)
    monkeypatch.setattr(agent_settings, "_login_problem", AsyncMock(return_value=None))
    monkeypatch.setattr(task_manager, "current_pool", lambda: None)


async def create(db, tmp_path):
    await settings.seed_defaults(db)
    await db.execute("INSERT INTO agent_runtimes(id,name,type,capabilities) VALUES "
                     "('rt-test-codex','Test Codex','codex',$1)",
                     list(profile_registry.runtimes.ADAPTERS['codex'].capabilities))
    await db.execute("INSERT INTO agent_profiles(id,name,runtime_id,model_id) "
                     "SELECT 'agent-codex','Test Codex','rt-test-codex',id FROM llm_models "
                     "WHERE provider_id='prov-codex-login' AND name='gpt-6.1-sol'")
    await db.execute("INSERT INTO agent_runtimes(id,name,type,capabilities) VALUES "
                     "('rt-missing-capabilities','Missing capabilities','claude-code','{}')")
    await db.execute("INSERT INTO agent_profiles(id,name,runtime_id,model_id) "
                     "SELECT 'profile-wrong-role','Wrong role','rt-missing-capabilities',model_id "
                     "FROM agent_profiles WHERE id='agent-claude-code-opus'")
    owner = await projects.create_project(name="Profile contract", work_dir=str(tmp_path), pool=db)
    await tasks.create_task(task_id="task-profile", project_id=owner["id"], requirement="", status="draft", pool=db)
    return owner


def test_readback_sources_choices_and_set(monkeypatch, tmp_path):
    async def check(db):
        await create(db, tmp_path)
        # None of the shared or Manager-specific selections may leak into this role.
        await agent_setting_layers.put_layer("system", None, None, {"profile_id": "agent-claude-code-opus"})
        await agent_setting_layers.put_layer("role", "manager", None, {"profile_id": "agent-claude-code-opus"})
        await agent_sessions.select_profile("manager", "agent-claude-code-opus", pool=db)
        async with v1_api(tmp_path, db) as call:
            status, body = await call("tasks.task_manager_profile_get", {"task_id": "task-profile"})
            assert status == 200, body
            default = body["result"]
            assert default["task_id"] == "task-profile" and default["override_profile_id"] is None
            assert default["source"] == "default"
            assert default["effective"] == {"profile_id": settings.DEFAULT_PROFILE_ID,
                "model": "claude-sonnet-5-5", "runtime": "claude-code",
                "provider": "prov-claude-subscription", "effort": None}
            accepted = []
            for row in await registry.list_profiles():
                try:
                    chosen = await profile_registry.check_selection(row["id"], ["task_manager"])
                except profile_registry.RegistryError:
                    continue
                accepted.append({"profile_id": chosen.id, "label": chosen.name,
                                 "model": row["model"]["name"], "runtime": chosen.runtime.type})
            assert default["choices"] == accepted
            assert "profile-wrong-role" not in {p["profile_id"] for p in accepted}
            assert {settings.DEFAULT_PROFILE_ID, "agent-claude-code-opus"} <= {p["profile_id"] for p in accepted}
            await agent_setting_layers.put_layer("role", "task_manager", None,
                                                {"profile_id": "agent-claude-code-opus"})
            await db.execute("UPDATE agent_profiles SET effort='high' WHERE id='agent-claude-code-opus'")
            status, body = await call("tasks.task_manager_profile_get", {"task_id": "task-profile"})
            assert status == 200 and body["result"]["source"] == "role", body
            assert body["result"]["effective"]["model"] == "opus"
            assert body["result"]["effective"]["effort"] == "high"
            assert body["result"]["override_profile_id"] is None
            status, body = await call("tasks.task_manager_profile_set", {
                "task_id": "task-profile", "profile_id": settings.DEFAULT_PROFILE_ID})
            assert status == 200 and body["result"]["source"] == "task", body
            assert body["result"]["override_profile_id"] == settings.DEFAULT_PROFILE_ID
            assert body["result"]["effective"]["model"] == "claude-sonnet-5-5"
            assert (await call("tasks.task_manager_profile_get", {"task_id": "task-profile"}))[1] == body
            status, body = await call("tasks.task_manager_profile_set", {"task_id": "task-profile", "profile_id": None})
            assert status == 200 and body["result"]["source"] == "role", body
            assert body["result"]["override_profile_id"] is None
    run_db(check, monkeypatch)


@pytest.mark.parametrize("operation", ["tasks.task_manager_profile_get", "tasks.task_manager_profile_set", "tasks.create_draft"])
def test_operator_and_work_mode_boundaries(monkeypatch, tmp_path, operation):
    async def check(db):
        owner = await create(db, tmp_path)
        arguments = ({"project_id": owner["id"]} if operation.endswith("create_draft") else
                     {"task_id": "task-profile", **({"profile_id": None} if operation.endswith("set") else {})})
        async with v1_api(tmp_path, db) as call:
            status, body = await call(operation, arguments, client=True)
            assert status == 403 and body["error"]["code"] == "forbidden", body
            task_conversations_settings.set_enabled(False)
            status, body = await call(operation, arguments)
            assert status == 409 and body["error"]["code"] == "conflict", body
            task_conversations_settings.set_enabled(True)
            assert (await call(operation, arguments))[0] == 200
    run_db(check, monkeypatch)


@pytest.mark.parametrize("operation", ["get", "set"])
def test_unknown_task(monkeypatch, tmp_path, operation):
    async def check(db):
        async with v1_api(tmp_path, db) as call:
            status, body = await call("tasks.task_manager_profile_" + operation,
                {"task_id": "missing", **({"profile_id": None} if operation == "set" else {})})
            assert status == 404 and body["error"]["code"] == "not_found", body
    run_db(check, monkeypatch)


@pytest.mark.parametrize("operation", ["tasks.task_manager_profile_get", "tasks.task_manager_profile_set", "tasks.create_draft"])
def test_manager_token_cannot_use_profile_operations(monkeypatch, tmp_path, operation):
    import asyncio
    from tests.test_api_v1_auth import _bearer, _send, _serving
    from tests.test_operator_auth import AGENT, TOKEN

    async def check():
        async with _serving(tmp_path) as server:
            status, _, body = await _send(server, "GET /api/v1/whoami", _bearer(TOKEN))
            assert status == 200 and body["result"]["principal"] == "operator"
            status, _, body = await _send(server, "POST /api/v1/" + operation.replace(".", "/"), _bearer(AGENT), "{}")
            assert status == 401 and body["error"]["code"] == "unauthorized", body
    asyncio.run(check())


def test_busy_is_typed_and_does_not_write(monkeypatch, tmp_path):
    async def check(db):
        await create(db, tmp_path)
        pool = task_manager.TaskManagerPool(tmp_path / "runtime", "unused", max_resident=lambda: 1)
        monkeypatch.setattr(task_manager, "current_pool", lambda: pool)
        pool.agent("task-profile").busy = True
        try:
            async with v1_api(tmp_path, db) as call:
                status, body = await call("tasks.task_manager_profile_set", {
                    "task_id": "task-profile", "profile_id": "agent-claude-code-opus"})
                assert status == 409 and body["error"]["code"] == "task_manager_busy", body
                assert await agent_setting_layers.profile_overrides("task-profile") == {}
                pool.agent("task-profile").busy = False
                assert (await call("tasks.task_manager_profile_set", {
                    "task_id": "task-profile", "profile_id": "agent-claude-code-opus"}))[0] == 200
        finally:
            await pool.shutdown()
    run_db(check, monkeypatch)


@pytest.mark.parametrize("choice,code", [("missing-profile", "invalid"), ("profile-wrong-role", "invalid"),
                                       ("agent-codex", "task_manager_runtime_unsupported")])
@pytest.mark.parametrize("operation", ["tasks.task_manager_profile_set", "tasks.create_draft"])
def test_invalid_selection_has_no_effect(monkeypatch, tmp_path, choice, code, operation):
    async def check(db):
        owner = await create(db, tmp_path)
        arguments = ({"project_id": owner["id"], "task_manager_profile_id": choice} if operation.endswith("create_draft")
                     else {"task_id": "task-profile", "profile_id": choice})
        async with v1_api(tmp_path, db) as call:
            status, body = await call(operation, arguments)
            assert status == 400 and body["error"]["code"] == code, body
            if code == "task_manager_runtime_unsupported":
                assert body["error"]["details"] == {"requested_runtime": "codex", "supported": "claude_code"}
            assert await db.fetchval("SELECT count(*) FROM tasks") == 1
            assert await agent_setting_layers.profile_overrides("task-profile") == {}
    run_db(check, monkeypatch)


@pytest.mark.parametrize("operation", ["tasks.task_manager_profile_set", "tasks.create_draft"])
def test_inherited_runtime_is_validated_before_clear_or_create(monkeypatch, tmp_path, operation):
    async def check(db):
        owner = await create(db, tmp_path)
        await agent_setting_layers.put_layer("role", "task_manager", None, {"profile_id": "agent-codex"})
        await agent_setting_layers.set_task_layer("task-profile", profiles={"task_manager": settings.DEFAULT_PROFILE_ID})
        arguments = ({"project_id": owner["id"]} if operation.endswith("create_draft") else
                     {"task_id": "task-profile", "profile_id": None})
        async with v1_api(tmp_path, db) as call:
            status, body = await call(operation, arguments)
            assert status == 400 and body["error"]["code"] == "task_manager_runtime_unsupported", body
            assert body["error"]["details"] == {"requested_runtime": "codex", "supported": "claude_code"}
            assert await db.fetchval("SELECT count(*) FROM tasks") == 1
            assert (await agent_setting_layers.profile_overrides("task-profile"))["task_manager"]["id"] == settings.DEFAULT_PROFILE_ID
    run_db(check, monkeypatch)


@pytest.mark.parametrize("choice", [None, "agent-claude-code-opus"])
def test_draft_profile_is_persisted_and_read_back(monkeypatch, tmp_path, choice):
    async def check(db):
        owner = await create(db, tmp_path)
        async with v1_api(tmp_path, db) as call:
            status, body = await call("tasks.create_draft", {"project_id": owner["id"],
                **({"task_manager_profile_id": choice} if choice else {})})
            assert status == 200, body
            result = body["result"]
            assert set(result) == {"task_id", "status", "created_at", "project_id", "workspace", "task_manager_profile"}
            assert result["status"] == "draft"
            view = result["task_manager_profile"]
            assert view["override_profile_id"] == choice and view["source"] == ("task" if choice else "default")
            assert view["effective"]["profile_id"] == (choice or settings.DEFAULT_PROFILE_ID)
            assert (await call("tasks.task_manager_profile_get", {"task_id": result["task_id"]}))[1]["result"] == view
            api_v1.validate_arguments(api_v1.find("POST", "/api/v1/tasks/create_draft").result, result)
    run_db(check, monkeypatch)
