"""The in-process API runtime's discovery, profile choices and role boundaries."""

from __future__ import annotations

import importlib.metadata
import logging
import shutil
from typing import Any

import pytest

from looper.agents import profile_registry as service, runtimes
from looper.agents.runtimes.api import ApiRuntime
from looper.db import profile_registry as db
from tests.db_support import DSN, run_db
from tests.test_profile_registry_db import _call

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")

MISSING_PACKAGE = "the openai Python package is not installed with this Looper; reinstall Looper"


@pytest.fixture
def api(monkeypatch: pytest.MonkeyPatch) -> ApiRuntime:
    runtime = ApiRuntime()
    monkeypatch.setitem(runtimes.ADAPTERS, "api", runtime)
    monkeypatch.setattr(service, "_login_modules", lambda: {})
    original = importlib.metadata.version
    monkeypatch.setattr(importlib.metadata, "version", lambda name: "3.37.0" if name == "openai" else original(name))
    return runtime


def _missing_package(monkeypatch: pytest.MonkeyPatch) -> None:
    def version(name: str) -> str:
        raise importlib.metadata.PackageNotFoundError(name)

    monkeypatch.setattr(importlib.metadata, "version", version)


async def _profile(api: ApiRuntime) -> dict[str, Any]:
    assert await service._detect(api)
    provider = (await service.create_provider({
        "name": "API account", "vendor_type": "custom", "api_key": "test-key",
        "endpoints": [{"protocol": "openai-responses", "base_url": "http://127.0.0.1:1/v1"}],
    }))["provider"]
    model = (await service.create_model({
        "provider_id": provider["provider_id"], "name": "test-model", "protocol": "openai-responses",
    }))["model"]
    return (await service.create_profile({
        "name": "API Manager", "runtime_id": "rt-api", "model_id": model["model_id"],
    }))["profile"]


def test_detects_in_process_runtime_without_searching_path(api, monkeypatch):
    def no_path_search(*args: Any) -> None:
        pytest.fail("an in-process runtime has no executable to find")

    monkeypatch.setattr(shutil, "which", no_path_search)

    async def test(pool):
        assert await service._detect(api)
        row = await db.get_runtime("rt-api")
        assert row["version"] == "openai 3.37.0"
        assert set(row["capabilities"]) == {c.value for c in api.capabilities}
        view = service.runtime_view(row)
        assert view["in_process"] is True and view["path"] is None
        assert view["roles"] == ["manager"]

    run_db(test, monkeypatch, timeout=30)


def test_missing_sdk_is_not_registered_and_logs_reason(api, monkeypatch, caplog):
    _missing_package(monkeypatch)

    async def test(pool):
        with caplog.at_level(logging.INFO, logger=service.__name__):
            assert await service._detect(api) is False
        assert await db.get_runtime("rt-api") is None
        assert f"Model API (SDK) not registered: {MISSING_PACKAGE}" in caplog.messages

    run_db(test, monkeypatch, timeout=30)


def test_redetect_missing_sdk_explains_package_failure(api, monkeypatch):
    _missing_package(monkeypatch)

    async def test(pool):
        with pytest.raises(service.RegistryNotFound) as exc:
            await service.probe_runtime("rt-api")
        assert str(exc.value) == MISSING_PACKAGE
        assert await db.get_runtime("rt-api") is None

    run_db(test, monkeypatch, timeout=30)


def test_api_profile_can_be_selected_for_the_manager(api, monkeypatch):
    async def test(pool):
        profile = await _profile(api)
        status, payload = await _call("PUT", "/api/agent-settings/roles/manager", {
            "profile_id": profile["profile_id"],
        })
        assert status == 200, payload
        assert profile["roles"] == ["manager"]
        assert await pool.fetchval(
            "SELECT profile_id FROM agent_setting_layers WHERE scope = 'role' AND role = 'manager'",
        ) == profile["profile_id"]

    run_db(test, monkeypatch, timeout=30)


@pytest.mark.parametrize("scope,role", [
    ("system", None), ("project", None),
    *( ("role", role) for role in ("planner", "worker", "reviewer") ),
    *( ("project-role", role) for role in ("planner", "worker", "reviewer") ),
    *( ("task", role) for role in ("planner", "worker", "reviewer") ),
])
def test_api_profile_is_refused_at_other_selection_layers(api, monkeypatch, scope, role):
    async def test(pool):
        profile = await _profile(api)
        body = {"profile_id": profile["profile_id"]}
        if scope == "system":
            path = "/api/agent-settings/system"
        elif scope == "role":
            path = f"/api/agent-settings/roles/{role}"
        else:
            status, project = await _call("POST", "/api/projects", {"name": "API role boundary"})
            assert status == 201, project
            project_id = project["project_id"]
            path = f"/api/projects/{project_id}/agent-settings"
            if scope == "project-role":
                path += f"/roles/{role}"
            elif scope == "task":
                status, payload = await _call("POST", "/api/tasks/create", {
                    "requirement": "Check role selection", "project_id": project_id,
                    "profile_overrides": {role: profile["profile_id"]},
                })
                assert status == 400 and "runs only the manager" in payload["error"], payload
                assert not await pool.fetchval("SELECT count(*) FROM tasks")
                return
        status, payload = await _call("PUT", path, body)
        assert status == 400 and "runs only the manager" in payload["error"], payload
        assert not await pool.fetchval("SELECT count(*) FROM agent_setting_layers WHERE profile_id = $1",
                                       profile["profile_id"])

    run_db(test, monkeypatch, timeout=30)


def test_api_profile_is_listed_in_manager_session_choices(api, monkeypatch):
    from looper.agents.agent_settings import AgentSettings
    from looper.agents.session_profile import choices

    async def test(pool):
        profile = await _profile(api)
        listed = await choices(None, AgentSettings())
        assert profile["profile_id"] in {choice.profile_id for choice in listed}

    run_db(test, monkeypatch, timeout=30)


@pytest.mark.parametrize("operation", ["choices", "selection", "stored-list"])
def test_project_profiles_honor_runtime_roles_over_stored_run_capability(api, monkeypatch, operation):
    async def test(pool):
        profile = await _profile(api)
        await pool.execute("UPDATE agent_runtimes SET capabilities = array_append(capabilities, 'run') "
                           "WHERE id = 'rt-api'")
        status, project = await _call("POST", "/api/projects", {"name": "Profile choices"})
        assert status == 201, project
        project_id = project["project_id"]
        path = f"/api/projects/{project_id}/allowed-profiles"
        if operation == "selection":
            status, payload = await _call("PUT", path, {"profile_ids": [profile["profile_id"]]})
            assert status == 400 and "can run neither the worker nor the reviewer" in payload["error"]
        else:
            if operation == "stored-list":
                await pool.execute("INSERT INTO project_allowed_profiles (project_id, profile_id) VALUES ($1, $2)",
                                   project_id, profile["profile_id"])
            status, payload = await _call("GET", path)
            assert status == 200, payload
            if operation == "choices":
                assert profile["profile_id"] not in {item["profile_id"] for item in payload["choosable"]}
            else:
                assert payload["profiles"][0]["roles"] == []

    run_db(test, monkeypatch, timeout=30)


@pytest.mark.parametrize("runtime_type", ["claude-code", "codex"])
def test_existing_runtime_keeps_stored_capabilities_and_unrestricted_roles(api, monkeypatch, runtime_type):
    monkeypatch.setattr(shutil, "which", lambda executable: "/runtime")

    async def test(pool):
        capabilities = [runtimes.Capability.RUN.value]
        await pool.execute(
            "INSERT INTO agent_runtimes (id, name, type, capabilities) VALUES ($1, $2, $2, $3) "
            "ON CONFLICT (id) DO UPDATE SET capabilities = EXCLUDED.capabilities",
            f"rt-{runtime_type}", runtime_type, capabilities,
        )
        assert await service._detect(runtimes.ADAPTERS[runtime_type]) is False
        row = await db.get_runtime(f"rt-{runtime_type}")
        view = service.runtime_view(row)
        assert view["capabilities"] == capabilities
        assert view["roles"] == ["worker", "reviewer"]
        assert service.runtime_spec(row).capabilities == frozenset({runtimes.Capability.RUN})
        assert view["in_process"] is False

    run_db(test, monkeypatch, timeout=30)


def test_config_import_explains_missing_in_process_runtime(api, monkeypatch):
    from looper.agents.config_transfer import run_import

    _missing_package(monkeypatch)

    async def test(pool):
        async with pool.acquire() as conn:
            plan = await run_import(conn, {
                "format": "looper-config/v3", "runtimes": [{"name": "api", "type": "api"}],
            }, apply=False)
        changes = plan.view()["changes"]
        assert changes == [{
            "section": "runtimes", "key": "api", "action": "skipped",
            "reason": f"runtimes are built in, and Model API (SDK) cannot run here: {MISSING_PACKAGE}; "
                      "then Re-detect it in Settings, Agent Runtimes",
        }]
        assert await db.get_runtime("rt-api") is None

    run_db(test, monkeypatch, timeout=30)
