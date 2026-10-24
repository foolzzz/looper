"""Authenticated global role settings operations, with disposable PostgreSQL readback."""

import json
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from looper.agents import agent_settings, api_v1
from looper.agents.bus import MessageBus
from looper.agents.ops.clients import ClientOps
from looper.agents.ops.service import Platform
from looper.db import llm_records, role_configs
from tests.db_support import DSN, run_db
from tests.test_api_v1_auth import CLIENT_TOKEN, _bearer, _send, _serving, clients
from tests.test_operator_auth import TOKEN

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")


@asynccontextmanager
async def _configured(tmp_path):
    await role_configs.seed_defaults([{"role": role, "system_prompt": "Recommended example"}
                                     for role in ("manager", "planner", "worker", "reviewer")])
    async with _serving(tmp_path) as server:
        bus = MessageBus(runtime_root=tmp_path)
        running = SimpleNamespace(terminate=AsyncMock(), cancel=AsyncMock())
        server._worker_pool = SimpleNamespace(recheck=AsyncMock(), running={"worker-active": running})
        server._client_ops = ClientOps(Platform(bus=bus, task_router=server._task_router,
                                               worker_pool=server._worker_pool, runtime_root=tmp_path))
        try:
            yield server
        finally:
            await bus.shutdown()


async def _post(server, path, data=None, *, operator=False):
    status, _, body = await _send(server, f"POST /api/v1/{path}",
                                 {**_bearer(TOKEN if operator else CLIENT_TOKEN), "X-Looper-Session": "settings"},
                                 json.dumps(data or {}))
    return status, body


def _result(operation, body):
    schema = next(op.result for op in api_v1.operations() if op.operation_id == operation)
    return api_v1.validate_arguments(schema, body["result"])


def test_role_list_get_update_and_recommended_prompt_reset(tmp_path, monkeypatch, clients):
    async def run(pool):
        async with _configured(tmp_path) as server:
            status, body = await _post(server, "roles/list")
            configs = _result("roles.list", body)["configs"]
            assert status == 200 and {r["role"] for r in configs} == {"manager", "planner", "worker", "reviewer"}
            status, body = await _post(server, "roles/update", {"role": "worker", "fields": {
                "system_prompt": "Edited example", "extra_args": {"flag": True}, "metadata": {"origin": "test"}}})
            assert status == 200 and _result("roles.update", body)["system_prompt"] == "Edited example"
            status, body = await _post(server, "roles/get", {"role": "worker"})
            assert status == 200 and _result("roles.get", body) == await role_configs.get_config("worker")
            status, body = await _post(server, "roles/reset_prompt", {"role": "worker"})
            config = _result("roles.reset_prompt", body)
            assert status == 200 and config["system_prompt"] == config["default_prompt"] == "Recommended example"
            assert (await role_configs.get_config("worker"))["system_prompt"] == "Recommended example"
    run_db(run, monkeypatch, timeout=30)


def test_reset_without_a_recommended_prompt_keeps_the_custom_prompt(tmp_path, monkeypatch, clients):
    async def run(pool):
        async with _configured(tmp_path) as server:
            await pool.execute("UPDATE role_configs SET default_prompt='', system_prompt='Custom example' WHERE role='worker'")
            status, body = await _post(server, "roles/reset_prompt", {"role": "worker"})
            assert status == 404 and body["error"]["code"] == "not_found"
            assert (await role_configs.get_config("worker"))["system_prompt"] == "Custom example"
    run_db(run, monkeypatch, timeout=30)


@pytest.mark.parametrize("field", ["unknown", "role", "default_prompt", "created_at", "updated_at",
                                     "runtime", "llm_profile_id", "harness_prompt", "model", "max_concurrency"])
def test_role_patch_rejects_unknown_readonly_removed_and_moved_fields(tmp_path, monkeypatch, clients, field):
    async def run(pool):
        async with _configured(tmp_path) as server:
            before = await role_configs.get_config("worker")
            status, body = await _post(server, "roles/update", {"role": "worker", "fields": {
                "system_prompt": "Must not save", field: "invalid"}})
            assert status == 400 and body["error"]["code"] == "invalid"
            assert field in body["error"]["message"] and "/api/v1/" in body["error"]["message"]
            assert await role_configs.get_config("worker") == before
    run_db(run, monkeypatch, timeout=30)


@pytest.mark.parametrize("clear_env", [None, []])
def test_global_layer_precedence_env_replacement_and_nullable_inheritance(tmp_path, monkeypatch, clients, clear_env):
    async def run(pool):
        async with _configured(tmp_path) as server:
            status, body = await _post(server, "agent_settings/update_system", {"values": {
                "harness_prompt": "Shared example", "run_budget_usd": 8,
                "env": [{"key": "MODE", "value": "shared"}, {"key": "BASE", "value": "on"}]}}, operator=True)
            assert status == 200 and _result("agent_settings.update_system", body)["system"]["values"]["run_budget_usd"] == 8
            status, body = await _post(server, "agent_settings/update_role", {"role": "worker", "values": {
                "harness_prompt": "Role example", "run_budget_usd": 3, "system_prompt_mode": "replace",
                "env": [{"key": "MODE", "value": "role"}, {"key": "BASE", "value": None}]}})
            role = _result("agent_settings.update_role", body)["roles"]["worker"]
            assert status == 200 and role["effective"]["harness_prompt"] == {"value": "Role example", "source": "role"}
            assert role["effective"]["env"] == [{"key": "MODE", "value": "role", "source": "role"}]
            assert role["effective"]["system_prompt_mode"] == "replace"
            status, body = await _post(server, "agent_settings/update_role", {"role": "worker", "values": {
                "harness_prompt": "", "run_budget_usd": "", "system_prompt_mode": None,
                "env": [{"key": "NEW", "value": "on"}]}}, operator=True)
            role = _result("agent_settings.update_role", body)["roles"]["worker"]
            assert status == 200 and role["values"]["harness_prompt"] is None
            assert role["values"]["env"] == [{"key": "NEW", "value": "on"}]
            assert role["effective"]["run_budget_usd"] == {"value": 8, "source": "system"}
            assert role["effective"]["harness_prompt"] == {"value": "Shared example", "source": "system"}
            assert role["effective"]["system_prompt_mode"] == "append"
            status, body = await _post(server, "agent_settings/update_role", {"role": "worker", "values": {"env": clear_env}})
            assert status == 200 and body["result"]["roles"]["worker"]["values"]["env"] == []
            assert {e["key"]: e["value"] for e in body["result"]["roles"]["worker"]["effective"]["env"]} == {"BASE": "on", "MODE": "shared"}
            status, body = await _post(server, "agent_settings/get")
            assert status == 200 and _result("agent_settings.get", body) == await agent_settings.settings_view()
    run_db(run, monkeypatch, timeout=30)


@pytest.mark.parametrize("path,values", [("update_system", {"harness_prompt": "Shared update"}),
    ("update_role", {"max_concurrency": 2}), ("update_role", {"run_budget_usd": 6}),
    ("update_role", {"run_budget_usd": None})])
def test_high_client_changes_wait_for_confirmation_and_operator_executes_directly(tmp_path, monkeypatch, clients, path, values):
    async def run(pool):
        await agent_settings.update_system({"run_budget_usd": 10})
        await agent_settings.update_role("worker", {"run_budget_usd": 4})
        async with _configured(tmp_path) as server:
            record_id = await llm_records.start_record(agent_id="worker-active")
            before = await agent_settings.settings_view()
            data = {"values": values, **({"role": "worker"} if path == "update_role" else {})}
            status, body = await _post(server, f"agent_settings/{path}", data)
            assert status == 202 and body["status"] == "needs_confirmation" and body["impact"]
            assert await agent_settings.settings_view() == before
            server._worker_pool.recheck.assert_not_awaited()
            confirmation = {"confirmation_id": body["confirmation_id"]}
            status, body = await _post(server, "confirm", confirmation)
            assert status == 200, body
            _result(f"agent_settings.{path}", body)
            if "max_concurrency" in values:
                server._worker_pool.recheck.assert_awaited_once()
                running = server._worker_pool.running["worker-active"]
                running.terminate.assert_not_awaited()
                running.cancel.assert_not_awaited()
                assert await pool.fetchval("SELECT status FROM llm_records WHERE id=$1", record_id) == "running"
                assert (await _post(server, "confirm", confirmation))[0] == 404
                server._worker_pool.recheck.assert_awaited_once()
            status, body = await _post(server, f"agent_settings/{path}", data, operator=True)
            assert status == 200 and body["status"] == "ok"
    run_db(run, monkeypatch, timeout=30)


def test_lowering_a_role_spend_cap_is_an_ordinary_write(tmp_path, monkeypatch, clients):
    async def run(pool):
        await agent_settings.update_role("worker", {"run_budget_usd": 5})
        async with _configured(tmp_path) as server:
            status, body = await _post(server, "agent_settings/update_role", {"role": "worker", "values": {"run_budget_usd": 2}})
            assert status == 200 and _result("agent_settings.update_role", body)["roles"]["worker"]["values"]["run_budget_usd"] == 2
    run_db(run, monkeypatch, timeout=30)


def test_existing_settings_set_returns_its_actual_typed_result(tmp_path, monkeypatch, clients):
    async def run(pool):
        async with _configured(tmp_path) as server:
            status, body = await _post(server, "ops/settings/set", {"role": "manager", "values": {"harness_prompt": "Example"}})
            result = _result("settings.set", body)
            assert status == 200 and result["layer"] == "role manager" and result["changes"][0]["setting"] == "harness_prompt"
            assert result["effective"]["manager"]["harness_prompt"]["source"] == "role"
            assert result["manager_session"] == {"selected": None}
    run_db(run, monkeypatch, timeout=30)


@pytest.mark.parametrize("legacy,path,data", [
    ("GET /api/roles", "roles/list", {}), ("GET /api/roles/worker", "roles/get", {"role": "worker"}),
    ("PUT /api/roles/worker", "roles/update", {"role": "worker", "fields": {"display_name": "Worker"}}),
    ("POST /api/roles/worker/reset-prompt", "roles/reset_prompt", {"role": "worker"}),
    ("GET /api/agent-settings", "agent_settings/get", {}),
    ("PUT /api/agent-settings/system", "agent_settings/update_system", {"values": {"harness_prompt": None}}),
    ("PUT /api/agent-settings/roles/worker", "agent_settings/update_role", {"role": "worker", "values": {"harness_prompt": None}}),
])
def test_retired_routes_are_404_with_authenticated_v1_positive_controls(tmp_path, monkeypatch, clients, legacy, path, data):
    async def run(pool):
        async with _configured(tmp_path) as server:
            status, body = await _post(server, path, data, operator=True)
            assert status == 200 and body["status"] == "ok"
            status, _, _ = await _send(server, legacy, _bearer(TOKEN), "{}")
            assert status == 404
    run_db(run, monkeypatch, timeout=30)
