"""Provider accounts over authenticated HTTP and disposable PostgreSQL.

Profile rows are seeded only to exercise counts and reference protection;
the Profile HTTP contract has its own end-to-end tests.
"""
from __future__ import annotations

import asyncio
import json
import socket
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from looper.agents import http_server, profile_registry as service
from looper.agents.runtimes import VendorType
from looper.db import connection, profile_registry as db
from tests.db_support import DSN, fresh_schema

needs_db = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")
SECRET = "sk-synthetic-provider-http-only"
TOKEN = "synthetic-operator-token"
URL = "https://api.example.invalid/v1"


class Client:
    def __init__(self, server):
        self.server = server

    async def call(self, method, path, body=None, *, token=TOKEN, raw=None, length=None):
        from tests.runtime_model_helpers import profile_request

        legacy_profile = path.startswith("/api/agent-profiles")
        method, path, body = profile_request(method, path, body)
        if method == "POST" and (path == "/api/v1/agent_settings/update_role" or path.startswith(("/api/v1/profiles/", "/api/v1/ops/models/", "/api/v1/models/"))):
            from looper.agents.ops.clients import ClientOps
            self.server._client_ops = ClientOps(SimpleNamespace())
        payload = raw if raw is not None else b"" if body is None else json.dumps(body).encode()
        # Real HTTP bytes through the production connection handler. A socket pair
        # needs no listening port and keeps the test independent of host services.
        client_socket, server_socket = socket.socketpair()
        client_socket.setblocking(False)
        server_socket.setblocking(False)
        reader, writer = await asyncio.open_connection(sock=client_socket)
        server_reader, server_writer = await asyncio.open_connection(sock=server_socket)
        serving = asyncio.create_task(self.server._handle_connection(server_reader, server_writer))
        headers = f"Host: 127.0.0.1:{self.server._port}\r\n"
        if token is not None:
            headers += f"Authorization: Bearer {token}\r\n"
        content_length = len(payload) if length is None else length
        headers += f"Content-Type: application/json\r\nContent-Length: {content_length}\r\n"
        try:
            writer.write(f"{method} {path} HTTP/1.1\r\n{headers}\r\n".encode() + payload)
            await writer.drain()
            response = await asyncio.wait_for(reader.read(), timeout=5)
        finally:
            writer.close()
            await writer.wait_closed()
            await serving
        head, _, data = response.partition(b"\r\n\r\n")
        assert SECRET.encode() not in data, data
        status, answer = int(head.split()[1]), json.loads(data)
        if path.startswith("/api/v1/provider_accounts/") and "status" in answer:
            answer = answer["result"] if answer["status"] == "ok" else {"error": answer["error"]["message"]}
        if legacy_profile:
            answer = answer["result"] if answer.get("status") == "ok" else {"error": answer["error"]["message"]}
            # Existing fixture callers assert the former creation status, while HTTP uses v1's 200.
            if path.endswith("/create") and status == 200:
                status = 201
        return status, answer

    async def ok(self, method, path, body=None, *, status=200):
        actual, answer = await self.call(method, path, body)
        assert actual == status, answer
        return answer["result"] if path == "/api/v1/registry/view" or path.startswith(("/api/v1/ops/models/", "/api/v1/models/")) else answer


def _run(test, monkeypatch, tmp_path, *, database=True):
    async def serve(pool):
        # Login state is external: never inspect the operator's CLI homes.
        monkeypatch.setattr(service, "_login_modules", lambda: {})
        server = http_server.AgentHTTPServer(
            port=0, web_adapter=SimpleNamespace(), task_router=SimpleNamespace(),
            supervisor=SimpleNamespace(), runtime_root=tmp_path / "runtime", operator_token=TOKEN)
        try:
            await test(Client(server), pool)
        finally:
            await server.stop()

    async def run():
        if database:
            async with fresh_schema() as pool:
                await connection.ensure_schema(pool)
                monkeypatch.setattr(connection, "_pool", pool)
                await serve(pool)
        else:
            from looper.db import manager_actions
            monkeypatch.setattr(manager_actions, "record", AsyncMock())
            await serve(None)

    asyncio.run(asyncio.wait_for(run(), timeout=45))


def _account(**changes):
    return {"name": "HTTP account", "vendor_type": "custom", "endpoints": [
        {"protocol": "anthropic-messages", "base_url": URL}], **changes}


@pytest.mark.parametrize("changes", [
    {"models": []}, {"protocols": ["openai-chat"]}, {"base_url": URL},
    {"auth": "oauth"}, {"oauth_runtime_type": "codex"},
    {"vendor_type": "unknown"}, {"max_concurrency": 0}, {"max_concurrency": True},
    {"max_concurrency": service.CONCURRENCY_MAX + 1},
    {"env": [{"key": "API_KEY", "value": SECRET}]},
    {"endpoints": [{"protocol": "openai-chat", "base_url": URL + "?api_key=" + SECRET}]},
    {"vendor_type": "codex-login", "endpoints": [{"protocol": "openai-chat", "base_url": ""}]},
])
def test_invalid_creation_is_rejected_before_database_access(monkeypatch, tmp_path, changes):
    async def test(client, pool):
        status, answer = await client.call("POST", "/api/v1/provider_accounts/create", _account(api_key=SECRET, **changes))
        assert status == 400 and set(answer) == {"error"}
    _run(test, monkeypatch, tmp_path, database=False)


@needs_db
def test_vendor_creation_registry_models_and_saved_defaults(monkeypatch, tmp_path):
    async def test(client, pool):
        registry = await client.ok("POST", "/api/v1/registry/view", {})
        assert {v["vendor_type"] for v in registry["vendor_types"]} == {v.value for v in VendorType}
        assert "provider_presets" not in registry
        assert registry["prices"] and registry["limits"]["secret_mask"] == "***"
        assert registry["runtime_types"] and registry["runtimes"] and registry["profiles"]
        deepseek = next(v for v in registry["vendor_types"] if v["vendor_type"] == "deepseek")
        assert deepseek["suggested_models"] and deepseek["endpoints"]
        account = (await client.ok("POST", "/api/v1/provider_accounts/create", {
            "name": "Vendor account", "vendor_type": "deepseek"}, status=200))["provider"]
        pid = account["provider_id"]
        assert account["auth"] == "api_key" and account["oauth_runtime_type"] is None
        assert account["api_key"] is None and account["login"] is None
        assert account["model_count"] == account["profile_count"] == 0
        assert account["endpoint_count"] == len(account["endpoints"]) > 0
        assert account["env"] == deepseek["env"]
        assert [{k: e[k] for k in ("protocol", "base_url", "use_runtime_default")}
                for e in account["endpoints"]] == deepseek["endpoints"]
        assert not {"models", "protocols", "base_url"} & account.keys()
        assert (await client.ok("POST", "/api/v1/ops/models/list", {"provider": pid}))["models"] == []
        # Positive control: a stored model must appear, rather than an always-empty response.
        protocol = account["endpoints"][0]["protocol"]
        await pool.execute("INSERT INTO llm_models (id, provider_id, name, protocol) VALUES "
                           "('model-http', $1, 'exact-http-model', $2)", pid, protocol)
        models = (await client.ok("POST", "/api/v1/ops/models/list", {"provider": pid}))["models"]
        assert [m["model_id"] for m in models] == ["model-http"]
        assert models[0]["protocol"] == protocol and models[0]["profile_count"] == 0
        registry = await client.ok("POST", "/api/v1/registry/view", {})
        assert next(m for m in registry["models"] if m["model_id"] == "model-http") == models[0]
        listed = next(p for p in (await client.ok("POST", "/api/v1/provider_accounts/list", {}))["providers"]
                      if p["provider_id"] == pid)
        assert listed["model_count"] == 1 and listed["endpoints"] == account["endpoints"]
        assert listed["endpoint_count"] == account["endpoint_count"]
        await client.ok("POST", "/api/v1/provider_accounts/update", {"provider_id": pid, "fields": {"env": [], "max_concurrency": 7}})
        renamed = (await client.ok("POST", "/api/v1/provider_accounts/update", {"provider_id": pid, "fields": {"name": "Renamed"}}))["provider"]
        assert renamed["env"] == [] and renamed["max_concurrency"] == 7
        assert renamed["endpoints"] == account["endpoints"]
    _run(test, monkeypatch, tmp_path)


@needs_db
def test_keys_masks_omission_replacement_and_null_clear(monkeypatch, tmp_path):
    async def test(client, pool):
        account = (await client.ok("POST", "/api/v1/provider_accounts/create", _account(api_key=SECRET,
            env=[{"key": "REGION", "value": "test"}]), status=200))["provider"]
        pid = account["provider_id"]
        assert account["api_key"] == "***" and account["env"] == [{"key": "REGION", "value": "test"}]
        for changes in ({"name": "Renamed"}, {"api_key": "***"}):
            updated = (await client.ok("POST", "/api/v1/provider_accounts/update", {"provider_id": pid, "fields": changes}))["provider"]
            assert updated["api_key"] == "***" and await db.get_api_key(pid) == SECRET
        await client.ok("POST", "/api/v1/provider_accounts/update", {"provider_id": pid, "fields": {"api_key": SECRET + "-replacement"}})
        assert await db.get_api_key(pid) == SECRET + "-replacement"
        for method, path in (("POST", "/api/v1/provider_accounts/list"), ("POST", "/api/v1/registry/view")):
            saved = next(p for p in (await client.ok(method, path, {}))["providers"] if p["provider_id"] == pid)
            assert saved["api_key"] == "***"
        cleared = (await client.ok("POST", "/api/v1/provider_accounts/update", {"provider_id": pid, "fields": {"api_key": None}}))["provider"]
        assert cleared["api_key"] is None and await db.get_api_key(pid) is None
        assert await client.ok("POST", "/api/v1/provider_accounts/delete", {"provider_id": pid}) == {"deleted": pid}
        assert await db.get_provider(pid) is None
    _run(test, monkeypatch, tmp_path)


@needs_db
def test_legacy_fields_vendor_changes_validation_and_error_envelopes(monkeypatch, tmp_path):
    async def test(client, pool):
        pid = (await client.ok("POST", "/api/v1/provider_accounts/create", _account(api_key=SECRET), status=200))["provider"]["provider_id"]
        for field, value in (("models", []), ("protocols", ["openai-chat"]), ("base_url", URL),
                             ("auth", "oauth"), ("oauth_runtime_type", "codex")):
            for method, path, body in (("POST", "/api/v1/provider_accounts/create", _account(**{field: value})),
                                       ("POST", "/api/v1/provider_accounts/update", {"provider_id": pid, "fields": {field: value, "api_key": SECRET}})):
                status, answer = await client.call(method, path, body)
                assert status == 400 and answer["error"] == "invalid credential request"
        for body in ({"vendor_type": "deepseek"}, {"max_concurrency": 0},
                     {"max_concurrency": True}, {"max_concurrency": service.CONCURRENCY_MAX + 1},
                     {"env": [{"key": "API_KEY", "value": SECRET}]},
                     {"endpoints": [{"protocol": "openai-chat", "base_url": URL + "?api_key=" + SECRET}]}, {}):
            status, answer = await client.call("POST", "/api/v1/provider_accounts/update", {"provider_id": pid, "fields": body})
            assert status == 400 and set(answer) == {"error"}
        for vendor in ("custom", "cpa", "opencode", "sub2api", "unknown"):
            status, answer = await client.call("POST", "/api/v1/provider_accounts/create", {"name": "Missing URL", "vendor_type": vendor})
            assert status == 400 and set(answer) == {"error"}
        status, answer = await client.call("POST", "/api/v1/provider_accounts/create", _account(api_key=SECRET))
        assert status == 409 and set(answer) == {"error"}
        status, answer = await client.call("POST", "/api/v1/provider_accounts/update", {"provider_id": 'prov-missing', "fields": {"name": "Missing"}})
        assert status == 404 and set(answer) == {"error"}
        assert await db.get_api_key(pid) == SECRET
        assert (await db.get_provider(pid))["vendor_type"] == "custom"
    _run(test, monkeypatch, tmp_path)


@needs_db
def test_login_ownership_and_problem_clear(monkeypatch, tmp_path):
    async def test(client, pool):
        modules = {kind: SimpleNamespace(clear_problem=AsyncMock(), login_view=AsyncMock(
            return_value={"state": "ready", "detail": f"synthetic {kind} login"})) for kind in ("codex", "grok")}
        monkeypatch.setattr(service, "_login_modules", lambda: modules)
        for vendor, runtime, protocol in (("claude-subscription", "claude-code", "anthropic-messages"),
                                           ("codex-login", "codex", "openai-responses"),
                                           ("grok-login", "grok", "openai-responses")):
            account = (await client.ok("POST", "/api/v1/provider_accounts/create", {
                "name": f"Test {vendor}", "vendor_type": vendor}, status=200))["provider"]
            assert account["auth"] == "oauth" and account["oauth_runtime_type"] == runtime
            assert account["api_key"] is None and account["model_count"] == 0
            assert account["endpoints"][0]["protocol"] == protocol and account["endpoints"][0]["base_url"] == ""
            if runtime in modules:
                assert account["login"] == modules[runtime].login_view.return_value
                cleared = (await client.ok("POST", "/api/v1/provider_accounts/clear_login_problem", {"provider_id": account['provider_id']}))["provider"]
                assert cleared["login"]["state"] == "ready"
                modules[runtime].clear_problem.assert_awaited_once()
            for changes in ({"api_key": SECRET}, {"endpoints": [{"protocol": protocol, "base_url": URL}]},
                            {"oauth_runtime_type": "other-runtime"}):
                status, answer = await client.call("POST", "/api/v1/provider_accounts/create", {
                    "name": "Invalid login", "vendor_type": vendor, **changes})
                assert status == 400 and set(answer) == {"error"}
        for pid in ("prov-claude-subscription", "prov-codex-login", "prov-grok-login"):
            for action, fields in (("update", {"name": "Changed"}), ("delete", None)):
                body = {"provider_id": pid, **({"fields": fields} if fields is not None else {})}
                status, answer = await client.call("POST", f"/api/v1/provider_accounts/{action}", body)
                assert status == 400 and answer["error"] == "invalid credential request"
        pid = (await client.ok("POST", "/api/v1/provider_accounts/create", _account(), status=200))["provider"]["provider_id"]
        status, answer = await client.call("POST", "/api/v1/provider_accounts/clear_login_problem", {"provider_id": pid})
        assert status == 400 and set(answer) == {"error"}
    _run(test, monkeypatch, tmp_path)


@needs_db
def test_model_profile_counts_and_reference_deletion_conflicts(monkeypatch, tmp_path):
    async def test(client, pool):
        pid = (await client.ok("POST", "/api/v1/provider_accounts/create", _account(), status=200))["provider"]["provider_id"]
        await pool.execute("INSERT INTO llm_models (id, provider_id, name, protocol) VALUES "
                           "('model-http', $1, 'http-model', 'anthropic-messages')", pid)
        await pool.execute("INSERT INTO agent_profiles (id, name, runtime_id, model_id) VALUES "
                           "('agent-http', 'HTTP profile', 'rt-claude-code', 'model-http')")
        for method, path in (("POST", "/api/v1/registry/view"), ("POST", "/api/v1/provider_accounts/list")):
            account = next(p for p in (await client.ok(method, path, {}))["providers"] if p["provider_id"] == pid)
            assert (account["model_count"], account["profile_count"]) == (1, 1)
        models = (await client.ok("POST", "/api/v1/ops/models/list", {"provider": pid, "runtime": 'rt-claude-code'}))["models"]
        assert len(models) == 1 and models[0]["profile_count"] == 1
        for action, fields in (("delete", None), ("update", {"endpoints": []})):
            body = {"provider_id": pid, **({"fields": fields} if fields is not None else {})}
            status, answer = await client.call("POST", f"/api/v1/provider_accounts/{action}", body)
            assert status == 409 and set(answer) == {"error"}
            assert (await db.get_provider(pid))["endpoints"][0]["base_url"] == URL
        await pool.execute("DELETE FROM agent_profiles WHERE id = 'agent-http'; "
                           "DELETE FROM llm_models WHERE id = 'model-http'")
        assert await client.ok("POST", "/api/v1/provider_accounts/delete", {"provider_id": pid}) == {"deleted": pid}
    _run(test, monkeypatch, tmp_path)


@pytest.mark.parametrize("method,path", [
    ("POST", "/api/v1/registry/view"), ("POST", "/api/v1/provider_accounts/list"), ("POST", "/api/v1/ops/models/list"),
    ("POST", "/api/v1/provider_accounts/create"), ("POST", "/api/v1/provider_accounts/update"),
    ("POST", "/api/v1/provider_accounts/delete"),
    ("POST", "/api/v1/provider_accounts/clear_login_problem"),
])
def test_operator_authentication_is_required(monkeypatch, tmp_path, method, path):
    async def test(client, pool):
        for token in (None, "wrong-token"):
            status, answer = await client.call(method, path, _account(api_key=SECRET), token=token)
            assert status == 401
            if path.startswith("/api/v1/") and not path.startswith("/api/v1/provider_accounts/"):
                assert answer["status"] == "error" and answer["error"]["code"] == "unauthorized"
            else:
                assert set(answer) == {"error"}
    _run(test, monkeypatch, tmp_path, database=False)


def test_request_body_limit_is_preserved(monkeypatch, tmp_path):
    async def test(client, pool):
        status, answer = await client.call("POST", "/api/v1/provider_accounts/create", {}, length=http_server.REQUEST_BODY_MAX_BYTES + 1)
        assert status == 413 and set(answer) == {"error"}
    _run(test, monkeypatch, tmp_path, database=False)


@pytest.mark.parametrize("raw", [b"{", b"[]", b"null"])
def test_invalid_json_uses_error_envelope(monkeypatch, tmp_path, raw):
    async def test(client, pool):
        status, answer = await client.call("POST", "/api/v1/provider_accounts/create", {}, raw=raw)
        assert status == 400 and set(answer) == {"error"}
    _run(test, monkeypatch, tmp_path, database=False)
