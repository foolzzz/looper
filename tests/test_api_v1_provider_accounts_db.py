"""Provider-account HTTP contracts on disposable PostgreSQL, including secret readback."""

import json
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from looper.agents import api_v1, profile_registry
from looper.agents.bus import MessageBus
from looper.agents.ops.clients import ClientOps
from looper.agents.ops.service import Platform
from looper.db import api_clients, profile_registry as db
from tests.db_support import DSN, run_db
from tests.test_api_v1_auth import _bearer, _send, _serving
from tests.test_operator_auth import TOKEN

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")
SECRET = "synthetic-provider-account-key"
URL = "https://provider.example.invalid"


@asynccontextmanager
async def _api(tmp_path, pool, monkeypatch):
    async def logins(providers):
        return {"codex": {"state": "chatgpt", "detail": "Synthetic login"},
                "grok": {"state": "unavailable", "detail": "Synthetic unavailable"}}
    monkeypatch.setattr(profile_registry, "_logins", logins)
    _, token = await api_clients.create("Provider validation", pool=pool)
    async with _serving(tmp_path) as server:
        server._client_ops = ClientOps(Platform(bus=MessageBus(runtime_root=tmp_path),
            task_router=server._task_router, runtime_root=tmp_path))

        async def call(operation, body=None, *, client=False, method="POST"):
            path = operation if operation.startswith("/") else "/api/v1/" + operation.replace(".", "/")
            status, _, answer = await _send(server, f"{method} {path}",
                _bearer(token if client else TOKEN), json.dumps(body or {}))
            if status == 200 and not operation.startswith("/"):
                api_v1.validate_arguments({"type": "object", "properties": {
                    "result": api_v1.find(method, path).result}}, {"result": answer["result"]})
            return status, answer
        yield call


def _body(**fields):
    return {"name": "Example account", "vendor_type": "custom", "api_key": SECRET,
            "endpoints": [{"protocol": "anthropic-messages", "base_url": URL}], **fields}


@pytest.mark.parametrize("fields,expected", [
    ({"name": "Renamed account"}, SECRET), ({"api_key": "***"}, SECRET),
    ({"api_key": None}, None), ({"api_key": "synthetic-replacement-key"}, "synthetic-replacement-key"),
])
def test_operator_key_updates_preserve_replace_clear_and_never_audit(tmp_path, monkeypatch, caplog, fields, expected):
    async def run(pool):
        async with _api(tmp_path, pool, monkeypatch) as call:
            status, answer = await call("provider_accounts.create", _body())
            assert status == 200, answer
            provider = answer["result"]["provider"]
            pid = provider["provider_id"]
            assert provider["api_key"] == "***" and "warnings" in answer["result"]
            status, answer = await call("provider_accounts.update", {"provider_id": pid,
                "fields": {"endpoints": provider["endpoints"], "max_concurrency": 7, **fields}})
            assert status == 200 and "warnings" in answer["result"], answer
            assert await db.get_api_key(pid) == expected
            assert (await db.get_provider(pid))["max_concurrency"] == 7
            assert answer["result"]["provider"]["api_key"] == ("***" if expected else None)
            status, snapshot = await call("provider_accounts.list")
            assert status == 200 and any(p["provider_id"] == pid for p in snapshot["result"]["providers"])
            assert SECRET not in json.dumps(snapshot) and SECRET not in json.dumps(answer)
            assert await pool.fetchval("SELECT count(*) FROM manager_actions") == 0
    run_db(run, monkeypatch, timeout=30)
    assert SECRET not in caplog.text and "synthetic-replacement-key" not in caplog.text


@pytest.mark.parametrize("fields", [
    {"max_concurrency": "7"},
    {"endpoints": [{"protocol": "invalid-protocol", "base_url": URL}]},
    {"endpoints": [{"protocol": "anthropic-messages", "base_url": URL}] * 2},
    {"env": [{"key": "ANTHROPIC_BASE_URL", "value": "https://owned.example.invalid"}]},
])
def test_invalid_updates_leave_name_endpoints_environment_and_secret_unchanged(tmp_path, monkeypatch, fields):
    async def run(pool):
        async with _api(tmp_path, pool, monkeypatch) as call:
            status, answer = await call("provider_accounts.create", _body())
            assert status == 200, answer
            pid = answer["result"]["provider"]["provider_id"]
            before = await db.get_provider(pid)
            status, answer = await call("provider_accounts.update", {"provider_id": pid,
                "fields": {"name": "Must roll back", "api_key": None, **fields}})
            assert status == 400 and answer["error"]["code"] == "invalid", answer
            assert await db.get_provider(pid) == before
            assert await db.get_api_key(pid) == SECRET
    run_db(run, monkeypatch, timeout=30)


def test_oauth_rejects_api_key_without_creating_an_account(tmp_path, monkeypatch):
    async def run(pool):
        async with _api(tmp_path, pool, monkeypatch) as call:
            count = await pool.fetchval("SELECT count(*) FROM llm_providers")
            status, answer = await call("provider_accounts.create", {"name": "Forbidden OAuth key",
                "vendor_type": "codex-login", "api_key": SECRET})
            assert status == 400 and answer["error"]["code"] == "invalid", answer
            assert await pool.fetchval("SELECT count(*) FROM llm_providers") == count
    run_db(run, monkeypatch, timeout=30)


@pytest.mark.parametrize("kind,status,code", [("builtin", 400, "invalid"), ("in-use", 409, "conflict")])
def test_provider_deletion_preserves_builtin_and_referenced_accounts(tmp_path, monkeypatch, kind, status, code):
    async def run(pool):
        pid = "prov-codex-login" if kind == "builtin" else "prov-unknown"
        async with _api(tmp_path, pool, monkeypatch) as call:
            if kind == "in-use":
                _, created = await call("provider_accounts.create", _body())
                pid = created["result"]["provider"]["provider_id"]
                await pool.execute("INSERT INTO llm_models (id, provider_id, name, protocol) "
                                   "VALUES ('model-example', $1, 'example', 'anthropic-messages')", pid)
            actual, answer = await call("provider_accounts.delete", {"provider_id": pid})
            assert actual == status and answer["error"]["code"] == code, answer
            assert await db.get_provider(pid) is not None
    run_db(run, monkeypatch, timeout=30)


@pytest.mark.parametrize("operation,body", [
    ("update", {"provider_id": "prov-missing", "fields": {"name": "Missing"}}),
    ("delete", {"provider_id": "prov-missing"}), ("clear_login_problem", {"provider_id": "prov-missing"}),
])
def test_unknown_provider_returns_not_found(tmp_path, monkeypatch, operation, body):
    async def run(pool):
        async with _api(tmp_path, pool, monkeypatch) as call:
            status, answer = await call("provider_accounts." + operation, body)
            assert status == 404 and answer["error"]["code"] == "not_found", answer
    run_db(run, monkeypatch, timeout=30)


def test_clear_login_problem_changes_only_selected_runtime_account(tmp_path, monkeypatch):
    states = {"codex": True, "grok": True}
    async def clear_codex():
        states["codex"] = False
    modules = {name: SimpleNamespace(clear_problem=AsyncMock(), login_view=AsyncMock(return_value={
        "state": "logged_in", "detail": "Synthetic login"})) for name in states}
    modules["codex"].clear_problem.side_effect = clear_codex
    monkeypatch.setattr(profile_registry, "_login_modules", lambda: modules)

    async def run(pool):
        async with _api(tmp_path, pool, monkeypatch) as call:
            before = await db.get_provider("prov-grok-login")
            status, answer = await call("provider_accounts.clear_login_problem", {"provider_id": "prov-codex-login"})
            assert status == 200 and answer["result"]["provider"]["provider_id"] == "prov-codex-login", answer
            modules["codex"].clear_problem.assert_awaited_once()
            modules["grok"].clear_problem.assert_not_awaited()
            assert states == {"codex": False, "grok": True}
            assert await db.get_provider("prov-grok-login") == before
    run_db(run, monkeypatch, timeout=30)


def test_compact_client_results_are_exact_and_still_refuse_keys(tmp_path, monkeypatch):
    async def run(pool):
        async with _api(tmp_path, pool, monkeypatch) as call:
            status, answer = await call("ops.providers.register", _body(api_key=None), client=True)
            assert status == 403 and answer["error"]["code"] == "forbidden", answer
            body = _body()
            del body["api_key"]
            status, answer = await call("ops.providers.register", body, client=True)
            assert status == 200 and answer["result"]["note"] and "warnings" in answer["result"], answer
            provider = answer["result"]["provider"]
            pid = provider["provider_id"]
            assert set(provider) == {"provider_id", "name", "vendor_type", "auth", "endpoints", "model_count",
                                     "profile_count", "builtin", "login", "max_concurrency", "api_key"}
            assert provider["api_key"].startswith("missing:")
            status, answer = await call("ops.providers.update", {"provider": pid, "api_key": SECRET}, client=True)
            assert status == 403 and answer["error"]["code"] == "forbidden", answer
            await profile_registry.update_provider(pid, {"api_key": SECRET})
            status, answer = await call("ops.providers.update", {"provider": pid, "name": "Compact account"}, client=True)
            assert status == 200 and answer["result"]["provider"]["api_key"] == "set", answer
            status, answer = await call("ops.providers.list", client=True)
            assert status == 200 and answer["result"]["vendors"] and answer["result"]["providers"], answer
            assert SECRET not in json.dumps(answer)
    run_db(run, monkeypatch, timeout=30)


@pytest.mark.parametrize("method,suffix,operation", [
    ("GET", "", "list"), ("POST", "", "create"), ("PUT", "/{id}", "update"),
    ("DELETE", "/{id}", "delete"), ("DELETE", "/prov-codex-login/login-problem", "clear_login_problem"),
])
def test_removed_routes_have_successful_corresponding_v1_controls(tmp_path, monkeypatch, method, suffix, operation):
    module = SimpleNamespace(clear_problem=AsyncMock(), login_view=AsyncMock(return_value={"state": "logged_in"}))
    monkeypatch.setattr(profile_registry, "_login_modules", lambda: {"codex": module})

    async def run(pool):
        async with _api(tmp_path, pool, monkeypatch) as call:
            _, created = await call("provider_accounts.create", _body())
            pid = created["result"]["provider"]["provider_id"]
            status, _ = await call("/api/providers" + suffix.format(id=pid), method=method)
            assert status == 404
            bodies = {"list": {}, "create": _body(name="Another account"),
                      "update": {"provider_id": pid, "fields": {"name": "Renamed account"}},
                      "delete": {"provider_id": pid}, "clear_login_problem": {"provider_id": "prov-codex-login"}}
            status, answer = await call("provider_accounts." + operation, bodies[operation])
            assert status == 200 and answer["result"], answer
    run_db(run, monkeypatch, timeout=30)
