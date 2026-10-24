"""Full registry projections and audited runtime probes on disposable PostgreSQL."""

import json
from contextlib import asynccontextmanager

import pytest

from looper.agents import api_v1, profile_registry, runtimes
from looper.agents.bus import MessageBus
from looper.agents.ops.clients import ClientOps
from looper.agents.ops.service import Platform
from looper.agents.runtimes import Capability, Probe
from looper.db import api_clients, profile_registry as db
from tests.db_support import DSN, run_db
from tests.test_api_v1_auth import _bearer, _send, _serving
from tests.test_operator_auth import TOKEN

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")


@asynccontextmanager
async def _api(tmp_path, pool, monkeypatch):
    async def logins(providers):
        return {"codex": {"state": "chatgpt", "detail": "Synthetic login"},
                "grok": {"state": "unavailable", "detail": "Synthetic unavailable"}}

    monkeypatch.setattr(profile_registry, "_logins", logins)
    _, token = await api_clients.create("Registry validation", pool=pool)
    async with _serving(tmp_path) as server:
        server._claude_bin = "configured-test-runtime"
        server._client_ops = ClientOps(Platform(bus=MessageBus(runtime_root=tmp_path),
            task_router=server._task_router, runtime_root=tmp_path))

        async def call(operation, body=None, *, client=False, method="POST"):
            path = operation if operation.startswith("/") else "/api/v1/" + operation.replace(".", "/")
            status, _, answer = await _send(server, f"{method} {path}",
                _bearer(token if client else TOKEN), json.dumps(body or {}))
            if status == 200 and not operation.startswith("/"):
                schema = api_v1.find(method, path).result
                api_v1.validate_arguments({"type": "object", "properties": {"result": schema}},
                                          {"result": answer["result"]})
            return status, answer

        yield call


def test_nonempty_full_snapshot_preserves_collections_secrets_prices_and_usage(tmp_path, monkeypatch):
    async def run(pool):
        provider = (await profile_registry.create_provider({
            "name": "Example account", "vendor_type": "custom", "api_key": "synthetic-api-key",
            "env": [{"key": "EXAMPLE_VALUE", "value": "synthetic-env-value"}],
            "endpoints": [{"protocol": "anthropic-messages", "base_url": "https://example.invalid"}],
        }))["provider"]
        await pool.execute("INSERT INTO managers (id,name,system_prompt,profile_id) "
                           "VALUES ('manager-example','Example','Reply briefly.','agent-claude-code-opus')")
        await pool.execute("INSERT INTO agent_sessions (agent_id,selected_profile_id) "
                           "VALUES ('manager-example','agent-claude-code-opus')")
        await pool.execute("INSERT INTO manager_task_templates (manager_id,key,name,worker_profile_id) "
                           "VALUES ('manager-example','check','Check','agent-claude-code-opus')")
        await pool.execute("INSERT INTO agent_setting_layers (scope, profile_id) VALUES ('system', 'agent-claude-code-opus')")
        async with _api(tmp_path, pool, monkeypatch) as call:
            status, answer = await call("registry.view")
            assert status == 200, answer
            view = answer["result"]
            assert set(view) == {"runtime_types", "runtimes", "providers", "models", "profiles", "vendor_types", "prices", "limits"}
            assert all(view.values())
            assert view["prices"]["models"] and view["prices"]["sources"]
            account = next(p for p in view["providers"] if p["provider_id"] == provider["provider_id"])
            assert account["api_key"] == "***"
            assert account["env"] == [{"key": "EXAMPLE_VALUE", "value": "synthetic-env-value"}]
            assert account["endpoints"][0]["base_url"] == "https://example.invalid"
            assert {p["login"]["state"] for p in view["providers"] if p["login"]} == {"chatgpt", "unavailable"}
            profile = view["profiles"][0]
            assert profile["used_by"]["layers"] == [{"scope": "system", "role": None, "project_id": None, "project_name": None}]
            assert profile["used_by"]["managers"] == ["Example"]
            assert profile["used_by"]["manager_sessions"] == ["manager-example"]
            assert profile["used_by"]["manager_templates"] == ["Check of Example"]
            assert "manager_sessions" in profile["used_by"] and "manager_templates" in profile["used_by"]
            assert profile["model"]["model_id"] == profile["model_id"]
            assert view["models"][0]["prices"]["input_usd_per_mtok"]["source"] == "builtin"
            assert view["limits"]["role_capabilities"]["manager"]
            assert await pool.fetchval("SELECT count(*) FROM manager_actions") == 0
    run_db(run, monkeypatch, timeout=30)


@pytest.mark.parametrize("client", [False, True], ids=["operator", "client"])
def test_probe_uses_configured_binary_and_persists_capabilities_without_confirmation(tmp_path, monkeypatch, client):
    observed = []

    def probe(executable):
        observed.append(executable)
        return Probe("synthetic-1.0", frozenset({Capability.RUN}), "Synthetic probe diagnostic")

    monkeypatch.setattr(runtimes.CLAUDE_CODE, "probe", probe)

    async def run(pool):
        async with _api(tmp_path, pool, monkeypatch) as call:
            status, answer = await call("runtimes.probe", {"runtime_id": "rt-claude-code"}, client=client)
            assert status == 200 and answer["status"] == "ok", answer
            runtime = answer["result"]["runtime"]
            assert runtime["version"] == "synthetic-1.0" and runtime["capabilities"] == ["run"]
            assert runtime["roles"] == ["worker", "reviewer"] and runtime["probed_at"]
            assert runtime["probe_error"] == "Synthetic probe diagnostic"
            row = await db.get_runtime("rt-claude-code")
            assert row["version"] == runtime["version"] and row["probe_error"] == runtime["probe_error"]
            assert observed == ["configured-test-runtime"]
            _, snapshot = await call("registry.view")
            assert next(p for p in snapshot["result"]["providers"] if p["provider_id"] == "prov-codex-login")["login"]["state"] == "chatgpt"
            audit = await pool.fetchrow("SELECT tool, risk, status FROM manager_actions")
            assert dict(audit) == {"tool": "runtimes.probe", "risk": "write", "status": "ok"}
    run_db(run, monkeypatch, timeout=30)


@pytest.mark.parametrize("failure,status,code", [
    (None, 404, "not_found"), (profile_registry.RegistryError, 400, "invalid"),
    (profile_registry.RegistryConflict, 409, "conflict"), (RuntimeError, 503, "unavailable"),
])
def test_probe_failure_is_typed_and_never_an_empty_success(tmp_path, monkeypatch, failure, status, code):
    if failure:
        async def fail(*args, **kwargs):
            raise failure("Synthetic probe failure")
        monkeypatch.setattr(profile_registry, "probe_runtime", fail)

    async def run(pool):
        async with _api(tmp_path, pool, monkeypatch) as call:
            actual, answer = await call("runtimes.probe", {"runtime_id": "rt-missing"}, client=True)
            assert actual == status and answer["status"] == "error", answer
            assert answer["error"]["code"] == code and "result" not in answer
            assert await pool.fetchval("SELECT count(*) FROM manager_actions WHERE status='error'") == 1
    run_db(run, monkeypatch, timeout=30)


@pytest.mark.parametrize("method,legacy,operation,body", [
    ("GET", "/api/registry", "registry.view", {}),
    ("POST", "/api/runtimes/rt-claude-code/probe", "runtimes.probe", {"runtime_id": "rt-claude-code"}),
])
def test_removed_routes_return_404_with_successful_v1_controls(tmp_path, monkeypatch, method, legacy, operation, body):
    monkeypatch.setattr(runtimes.CLAUDE_CODE, "probe", lambda exe: Probe("synthetic-1.0", frozenset({Capability.RUN})))

    async def run(pool):
        async with _api(tmp_path, pool, monkeypatch) as call:
            assert (await call(legacy, method=method))[0] == 404
            status, answer = await call(operation, body)
            assert status == 200 and answer["result"], answer
    run_db(run, monkeypatch, timeout=30)
