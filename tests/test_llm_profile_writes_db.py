"""Profile writes use exact model identities and preserve every selection reference."""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest

from looper.agents import profile_registry as service
from looper.agents import runtimes
from looper.db import agent_setting_layers, profile_registry as db
from tests.db_support import DSN, run_db

needs_db = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")


def _body(**changes):
    return {"name": "Test profile", "runtime_id": "rt-claude-code", "model_id": "model-messages", **changes}


def _row(**changes):
    return {"id": "agent-test", "name": "Test profile", "model_id": "model-messages",
            "runtime_id": "rt-claude-code", "runtime_name": "claude-code", "runtime_type": "claude-code",
            "runtime_capabilities": [c.value for c in runtimes.CLAUDE_CODE.capabilities],
            "effort": "high", "agent_teams": True, "custom_prompt": "instructions", **changes}


def _run(test, monkeypatch):
    async def setup(pool):
        monkeypatch.setattr(service, "_login_modules", lambda: {})
        # Target schema fixtures do not depend on the profile write path.
        await pool.execute("""
            INSERT INTO llm_providers (id, name, vendor_type, auth) VALUES
              ('prov-test', 'Test account', 'custom', 'api_key'),
              ('prov-other', 'Other account', 'custom', 'api_key');
            INSERT INTO llm_provider_endpoints (provider_id, protocol, base_url) VALUES
              ('prov-test', 'anthropic-messages', 'https://messages.example.invalid'),
              ('prov-test', 'openai-chat', 'https://chat.example.invalid'),
              ('prov-test', 'openai-responses', 'https://responses.example.invalid'),
              ('prov-other', 'anthropic-messages', 'https://other.example.invalid');
            INSERT INTO llm_models (id, provider_id, name, protocol, efforts) VALUES
              ('model-messages', 'prov-test', 'same-name', 'anthropic-messages', ARRAY['low']),
              ('model-chat', 'prov-test', 'same-name', 'openai-chat', NULL),
              ('model-responses', 'prov-test', 'same-name', 'openai-responses', '{}'),
              ('model-other', 'prov-other', 'same-name', 'anthropic-messages', NULL);
        """)
        for adapter in runtimes.ADAPTERS.values():
            await pool.execute("INSERT INTO agent_runtimes (id, name, type, executable, capabilities) "
                               "VALUES ($1, $2, $2, '', $3) ON CONFLICT (id) DO NOTHING",
                               service.builtin_runtime_id(adapter), adapter.type,
                               [c.value for c in adapter.capabilities])

    run_db(test, monkeypatch, setup=setup, timeout=30)


@pytest.mark.parametrize("field", ["provider_id", "model", "system_prompt_mode", "system_prompt_mode_choice",
                                  "mode", "protocol", "efforts", "builtin"])
@pytest.mark.parametrize("operation", ["create", "update"])
def test_writes_reject_obsolete_and_model_owned_fields(field, operation):
    async def run():
        with pytest.raises(service.RegistryError, match="unknown field"):
            if operation == "create":
                await service.create_profile(_body(**{field: "legacy"}))
            else:
                await service.update_profile("agent-test", {field: "legacy"})
    asyncio.run(asyncio.wait_for(run(), timeout=2))


def test_profile_readback_and_record_keep_only_model_identity():
    profile = service.AgentProfile.from_row(_row())
    assert isinstance(profile, service.AgentProfile)
    assert profile.model_id == "model-messages" and profile.effort == "high"
    assert profile.agent_teams is True
    assert profile.record() == {"id": "agent-test", "name": "Test profile", "model_id": "model-messages",
                                "custom_prompt": "instructions", "runtime": {
                                    "id": "rt-claude-code", "name": "claude-code", "type": "claude-code",
                                    "launcher": "native"}}


@pytest.mark.parametrize("role", runtimes.ROLE_CAPABILITIES)
def test_selection_uses_target_profile_and_checks_role_capabilities(role, monkeypatch):
    row = _row()
    monkeypatch.setattr(db, "get_profile", AsyncMock(return_value=row))
    async def run():
        profile = await service.check_selection("agent-test", [role])
        assert isinstance(profile, service.AgentProfile) and profile.model_id == "model-messages"
        row["runtime_capabilities"] = []
        with pytest.raises(service.RegistryError, match=role):
            await service.check_selection("agent-test", [role])
    asyncio.run(asyncio.wait_for(run(), timeout=2))


# Each runtime is checked against API-key models of all three exact protocols,
# plus all three built-in login owners. No provider protocol intersection fallback.
@pytest.mark.parametrize("runtime_type,accepted", [
    ("claude-code", {"messages", "claude-subscription"}),
    ("codex", {"responses", "codex-login"}),
    ("omp", {"messages", "chat", "responses"}),
    ("grok", {"grok-login"}),
    ("api", {"responses"}),
])
@needs_db
def test_create_and_update_enforce_protocol_auth_and_login_owner(runtime_type, accepted, monkeypatch):
    async def test(pool):
        models = {key: f"model-{key}" for key in ("messages", "chat", "responses")}
        for owner in ("claude-subscription", "codex-login", "grok-login"):
            models[owner] = await pool.fetchval("SELECT id FROM llm_models WHERE provider_id = $1 "
                                               "ORDER BY catalog_order, id LIMIT 1", f"prov-{owner}")
            assert models[owner] is not None
        runtime_id = service.builtin_runtime_id(runtimes.adapter(runtime_type))
        for key, model_id in models.items():
            data = _body(name=f"{runtime_type}-{key}", runtime_id=runtime_id, model_id=model_id)
            if key not in accepted:
                with pytest.raises(service.RegistryError):
                    await service.create_profile(data)
                continue
            profile = (await service.create_profile(data))["profile"]
            assert profile["runtime_id"] == runtime_id and profile["model_id"] == model_id
            assert profile["provider_id"] == (await db.get_model(model_id))["provider_id"]
            assert profile["agent_teams"] is (runtime_type == "claude-code")
            for target, target_id in models.items():
                before = await db.get_profile(profile["profile_id"])
                if target in accepted:
                    updated = (await service.update_profile(profile["profile_id"], {"model_id": target_id}))["profile"]
                    assert updated["model_id"] == target_id
                    assert updated["provider_id"] == (await db.get_model(target_id))["provider_id"]
                else:
                    with pytest.raises(service.RegistryError):
                        await service.update_profile(profile["profile_id"], {"model_id": target_id})
                    assert await db.get_profile(profile["profile_id"]) == before
    _run(test, monkeypatch)


@needs_db
@pytest.mark.parametrize("changes", [{"runtime_id": "missing"}, {"runtime_id": None}, {"runtime_id": 1},
                                     {"model_id": "missing"}, {"model_id": None}, {"model_id": 1},
                                     {"effort": "ultra"}, {"effort": True}, {"agent_teams": "false"},
                                     {"custom_prompt": None}, {"custom_prompt": "bad\x00prompt"}])
def test_invalid_writes_roll_back(changes, monkeypatch):
    async def test(pool):
        original = (await service.create_profile(_body()))["profile"]
        profile_id = original["profile_id"]
        before = await db.get_profile(profile_id)
        with pytest.raises(service.RegistryError):
            await service.create_profile(_body(name="Invalid profile", **changes))
        with pytest.raises(service.RegistryError):
            await service.update_profile(profile_id, changes)
        assert await db.get_profile(profile_id) == before
        assert {r["id"] for r in await db.list_profiles()} == {"agent-claude-code-opus", profile_id}
    _run(test, monkeypatch)


@needs_db
def test_runtime_switch_preserves_model_and_explicit_teams_rules(monkeypatch):
    async def test(pool):
        profile_id = (await service.create_profile(_body()))["profile"]["profile_id"]
        switched = (await service.update_profile(profile_id, {"runtime_id": "rt-omp"}))["profile"]
        assert switched["model_id"] == "model-messages" and switched["agent_teams"] is False
        before = await db.get_profile(profile_id)
        # Codex has saved models available, but switching must fail for this exact model.
        with pytest.raises(service.RegistryError):
            await service.update_profile(profile_id, {"runtime_id": "rt-codex"})
        assert await db.get_profile(profile_id) == before
        for operation in (lambda: service.create_profile(_body(name="Teams", runtime_id="rt-omp", agent_teams=True)),
                          lambda: service.update_profile(profile_id, {"agent_teams": True})):
            with pytest.raises(service.RegistryError):
                await operation()
        switched = (await service.update_profile(profile_id, {"runtime_id": "rt-claude-code"}))["profile"]
        assert switched["model_id"] == "model-messages" and switched["agent_teams"] is True
        explicit = (await service.update_profile(profile_id, {"runtime_id": "rt-omp", "agent_teams": False}))["profile"]
        assert explicit["agent_teams"] is False
    _run(test, monkeypatch)


@needs_db
def test_effort_metadata_is_advisory_and_options_round_trip(monkeypatch):
    async def test(pool):
        profile = (await service.create_profile(_body(effort="max", custom_prompt="  instructions  ")))["profile"]
        profile_id = profile["profile_id"]
        assert profile["effort"] == "max" and profile["custom_prompt"] == "instructions"
        for effort in ("low", "medium", "high", "xhigh", "max", None, ""):
            updated = (await service.update_profile(profile_id, {"effort": effort}))["profile"]
            assert updated["effort"] == (effort or None)
        updated = (await service.update_profile(profile_id, {"name": "Renamed", "model_id": "model-other"}))["profile"]
        assert updated["profile_id"] == profile_id and updated["provider_id"] == "prov-other"
        assert updated["custom_prompt"] == "instructions"
        assert (await service.update_profile(profile_id, {"custom_prompt": ""}))["profile"]["custom_prompt"] == ""
    _run(test, monkeypatch)


async def _references(pool, profile_id, kind):
    await pool.execute("INSERT INTO projects (id, name) VALUES ('proj-test', 'Test project'); "
                       "INSERT INTO tasks (id, requirement, project_id) VALUES ('task-test', 'Test task', 'proj-test'); "
                       "INSERT INTO schedules (id, project_id, name, cron, timezone, requirement) "
                       "VALUES ('sched-test', 'proj-test', 'Test schedule', '0 * * * *', 'UTC', 'Test run')")
    if kind in ("system", "role", "project", "role_project"):
        role = "planner" if kind in ("role", "role_project") else None
        project = "proj-test" if kind in ("project", "role_project") else None
        await agent_setting_layers.put_layer(kind, role, project, {"profile_id": profile_id}, pool=pool)
    elif kind == "task":
        await pool.execute("INSERT INTO task_profile_overrides VALUES ('task-test', 'planner', $1)", profile_id)
    elif kind == "schedule":
        await pool.execute("INSERT INTO schedule_profile_overrides VALUES ('sched-test', 'planner', $1)", profile_id)
    else:
        await pool.execute("INSERT INTO project_allowed_profiles (project_id, profile_id) VALUES ('proj-test', $1)",
                           profile_id)


@needs_db
@pytest.mark.parametrize("kind", ["system", "role", "project", "role_project", "task", "schedule", "allowed"])
def test_selected_profile_identity_usage_and_deletion_protection(kind, monkeypatch):
    async def test(pool):
        profile_id = (await service.create_profile(_body()))["profile"]["profile_id"]
        await _references(pool, profile_id, kind)
        before = await db.profile_usage(profile_id)
        assert profile_id in before
        if kind in ("system", "role", "project", "role_project"):
            assert before[profile_id]["layers"][0]["scope"] == kind
        elif kind == "task":
            assert before[profile_id]["task_count"] == 1
        elif kind == "schedule":
            assert before[profile_id]["schedule_count"] == 1
        else:
            assert before[profile_id]["allowed_in"] == ["Test project"]
        updated = (await service.update_profile(profile_id, {"name": "Renamed", "model_id": "model-other"}))["profile"]
        assert updated["profile_id"] == profile_id and updated["model_id"] == "model-other"
        assert updated["used_by"] == before[profile_id]
        assert await db.profile_usage(profile_id) == before
        selected = await service.check_selection(profile_id, ["planner", "worker", "reviewer"])
        assert selected.id == profile_id and selected.model_id == "model-other"
        for operation in (service.deletable_profile, service.delete_profile):
            with pytest.raises(service.RegistryConflict):
                await operation(profile_id)
        assert await db.get_profile(profile_id) is not None
    _run(test, monkeypatch)


@needs_db
@pytest.mark.parametrize("kind", ["system", "role", "project", "role_project", "task", "schedule"])
def test_selected_roles_revalidate_before_runtime_change(kind, monkeypatch):
    async def test(pool):
        profile_id = (await service.create_profile(_body()))["profile"]["profile_id"]
        await _references(pool, profile_id, kind)
        await pool.execute("UPDATE agent_runtimes SET capabilities = ARRAY['run'] WHERE id = 'rt-omp'")
        before = await db.get_profile(profile_id)
        with pytest.raises(service.RegistryError, match="planner|manager"):
            await service.update_profile(profile_id, {"runtime_id": "rt-omp"})
        assert await db.get_profile(profile_id) == before
        await pool.execute("UPDATE agent_runtimes SET capabilities = $1 WHERE id = 'rt-omp'",
                           [c.value for c in runtimes.adapter("omp").capabilities])
        assert (await service.update_profile(profile_id, {"runtime_id": "rt-omp"}))["profile"]["runtime_id"] == "rt-omp"
    _run(test, monkeypatch)


@needs_db
def test_builtin_duplicate_missing_and_unselected_deletion_contracts(monkeypatch):
    async def test(pool):
        builtin = "agent-claude-code-opus"
        for operation in (lambda: service.update_profile(builtin, {"name": "Changed"}),
                          lambda: service.deletable_profile(builtin), lambda: service.delete_profile(builtin)):
            with pytest.raises(service.RegistryError):
                await operation()
        profile_id = (await service.create_profile(_body()))["profile"]["profile_id"]
        other = (await service.create_profile(_body(name="Other profile")))["profile"]["profile_id"]
        with pytest.raises(service.RegistryConflict):
            await service.create_profile(_body())
        with pytest.raises(service.RegistryConflict):
            await service.update_profile(other, {"name": "Test profile"})
        assert (await db.get_profile(other))["name"] == "Other profile"
        assert (await service.deletable_profile(profile_id))["model_id"] == "model-messages"
        assert await service.delete_profile(profile_id) == {"deleted": profile_id}
        assert await db.get_profile(profile_id) is None and await db.get_model("model-messages") is not None
        for operation in (service.deletable_profile, service.delete_profile):
            with pytest.raises(service.RegistryNotFound):
                await operation(profile_id)
        with pytest.raises(service.RegistryNotFound):
            await service.update_profile(profile_id, {"effort": "high"})
    _run(test, monkeypatch)


@needs_db
def test_active_planner_choice_blocks_delete_until_task_ends(monkeypatch):
    async def test(pool):
        profile_id = (await service.create_profile(_body()))["profile"]["profile_id"]
        # project_id is NOT NULL after tasks_project_backfill. A task that has not
        # started is status 'init' (db.tasks.READY); 'ready' is only the dashboard group.
        await pool.execute("INSERT INTO projects (id, name) VALUES ('proj-test', 'Test project'); "
                           "INSERT INTO tasks (id, requirement, project_id, status) "
                           "VALUES ('task-test', 'Test task', 'proj-test', 'init')")
        await pool.execute("INSERT INTO task_planner_profiles (task_id, role, profile_id) "
                           "VALUES ('task-test', 'worker', $1)", profile_id)
        with pytest.raises(service.RegistryConflict):
            await service.delete_profile(profile_id)
        assert await db.get_profile(profile_id) is not None
        await pool.execute("UPDATE tasks SET status = 'completed' WHERE id = 'task-test'")
        assert await service.delete_profile(profile_id) == {"deleted": profile_id}
        assert await pool.fetchval("SELECT count(*) FROM task_planner_profiles") == 0
    _run(test, monkeypatch)
