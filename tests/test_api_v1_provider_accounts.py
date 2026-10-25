"""Operator credential boundary and generated provider-account requests."""

import asyncio
import json
import subprocess
import sys
from types import SimpleNamespace

import pytest

from looper import loopercli
from looper.agents import api_v1, profile_registry
from looper.agents.ops import OpError
from tests.test_api_v1_auth import CLIENT_TOKEN, _bearer, _send, _serving, clients as clients
from tests.test_operator_auth import TOKEN

ACTIONS = ("list", "create", "update", "delete", "clear_login_problem")


@pytest.mark.parametrize("action", ACTIONS)
@pytest.mark.parametrize("body", ["not-json", '{"api_key":"synthetic-secret","extra":true}'])
def test_client_refused_before_validation_service_or_audit(tmp_path, monkeypatch, clients, action, body):
    def forbidden(*args, **kwargs):
        pytest.fail("credential requests must be refused before validation, service or audit")

    monkeypatch.setattr(api_v1, "validate_arguments", forbidden)
    for name in ("list_providers", "create_provider", "update_provider", "delete_provider", "clear_login_problem"):
        monkeypatch.setattr(profile_registry, name, forbidden)

    async def run():
        async with _serving(tmp_path) as server:
            monkeypatch.setattr(server, "_v1_body", forbidden)
            server._client_ops = SimpleNamespace(call=forbidden)
            status, _, answer = await _send(server, f"POST /api/v1/provider_accounts/{action}",
                                            _bearer(CLIENT_TOKEN), body)
            assert status == 403 and answer["error"]["code"] == "forbidden"
    asyncio.run(run())


@pytest.mark.parametrize("action,body", [
    ("list", {"extra": True}), ("create", {"name": "Account"}),
    ("create", {"name": "Account", "vendor_type": "custom", "extra": True}),
    ("update", {"provider_id": "prov-example", "fields": {}}),
    ("update", {"provider_id": "prov-example", "fields": {"extra": True}}),
    ("delete", {"provider_id": None}), ("clear_login_problem", {"provider_id": 3}),
])
def test_requests_reject_missing_identifiers_unknown_fields_and_empty_patch(action, body):
    operation = api_v1.find("POST", f"/api/v1/provider_accounts/{action}")
    with pytest.raises(OpError):
        api_v1.validate_arguments(operation.request, body)


@pytest.mark.parametrize("action,options,body", [
    ("list", [], {}),
    ("create", ["--name", "Account", "--vendor-type", "custom"], {"name": "Account", "vendor_type": "custom"}),
    ("update", ["--provider-id", "prov-example", "--fields", '{"api_key":null}'],
     {"provider_id": "prov-example", "fields": {"api_key": None}}),
    ("delete", ["--provider-id", "prov-example"], {"provider_id": "prov-example"}),
    ("clear_login_problem", ["--provider-id", "prov-example"], {"provider_id": "prov-example"}),
])
def test_generated_cli_preserves_request_shape_without_server_imports(action, options, body, tmp_path):
    document = api_v1.document()
    args = loopercli.build_parser(document).parse_args(["provider-accounts", action.replace("_", "-"), *options])
    calls = []
    api = SimpleNamespace(call=lambda *parts: (calls.append(parts) or (200, {"status": "ok", "result": {}})))
    assert args._run(args, SimpleNamespace(api=lambda: api, as_json=True, session="provider-test", operator=True)) == 0
    assert calls == [("POST", f"/api/v1/provider_accounts/{action}", body)]
    path = tmp_path / "openapi.json"
    path.write_text(json.dumps(document))
    source = """import json,sys
from looper import loopercli
args = loopercli.build_parser(json.load(open(sys.argv[1]))).parse_args(sys.argv[2:])
assert callable(args._run)
assert not any(m.startswith(('looper.agents','asyncpg','httpx')) for m in sys.modules)
"""
    subprocess.run([sys.executable, "-c", source, str(path), "provider-accounts", action.replace("_", "-"), *options],
                   check=True, timeout=10)


def test_credential_operations_are_operator_only_without_new_manager_tools():
    for action in ACTIONS:
        operation = api_v1.find("POST", f"/api/v1/provider_accounts/{action}")
        assert operation.operator_only and operation.credential_write and not operation.hand
        assert operation.tool is None and not operation.confirms
    schemas = api_v1.document()["components"]["schemas"]
    for name in ("ProviderCreate", "ProviderPatch"):
        assert schemas[name]["properties"]["api_key"]["writeOnly"] is True
    endpoint = {"provider_id": "prov-example", "protocol": "anthropic-messages",
                "base_url": "https://example.invalid", "use_runtime_default": False}
    operation = api_v1.find("POST", "/api/v1/provider_accounts/update")
    api_v1.validate_arguments(operation.request, {"provider_id": "prov-example", "fields": {"endpoints": [endpoint]}})


def test_unexpected_credential_failure_never_logs_raw_exception_or_request(tmp_path, monkeypatch, caplog):
    secret = "synthetic-credential-exception-value"

    async def fail(data):
        raise RuntimeError(secret)

    monkeypatch.setattr(profile_registry, "create_provider", fail)

    async def run():
        async with _serving(tmp_path) as server:
            status, _, answer = await _send(server, "POST /api/v1/provider_accounts/create", _bearer(TOKEN),
                json.dumps({"name": "Account", "vendor_type": "custom", "api_key": secret}))
            assert status == 503 and answer["error"]["code"] == "unavailable"
            assert secret not in json.dumps(answer)
    asyncio.run(run())
    assert secret not in caplog.text


@pytest.mark.parametrize("case,status,message", [
    ("duplicate", 409, "provider account conflict"),
    ("in-use", 409, "provider account conflict"),
    ("profile", 409, "provider account conflict"),
    ("validation", 400, "invalid provider account request"),
    ("missing", 404, "provider not found"),
])
def test_public_provider_refusals_cross_transport(tmp_path, monkeypatch, caplog, case, status, message):
    from tests.db_support import run_db

    monkeypatch.setattr(profile_registry, "_login_modules", lambda: {})
    secret = "synthetic-public-error-credential"

    async def run(pool):
        async with _serving(tmp_path, listen=True) as server:
            async def call(action, body):
                code, headers, answer = await _send(server, "POST /api/v1/provider_accounts/" + action,
                                                   _bearer(TOKEN), json.dumps(body))
                assert headers["cache-control"] == "no-store"
                assert secret not in json.dumps(answer)
                return code, answer
            body = {"name": "Example", "vendor_type": "custom", "api_key": secret,
                    "endpoints": [{"protocol": "anthropic-messages", "base_url": "https://example.invalid"}]}
            code, created = await call("create", body)
            assert code == 200 and created["result"]["provider"]["api_key"] == "***"
            pid = created["result"]["provider"]["provider_id"]
            if case in ("in-use", "profile"):
                model = await profile_registry.create_model({"provider_id": pid, "name": "example",
                                                             "protocol": "anthropic-messages"})
                await profile_registry.create_profile({"name": "Example profile", "runtime_id": "rt-claude-code",
                                                       "model_id": model["model"]["model_id"]})
            action, request = {
                "duplicate": ("create", body), "in-use": ("delete", {"provider_id": pid}),
                "profile": ("update", {"provider_id": pid, "fields": {"endpoints": []}}),
                "validation": ("update", {"provider_id": pid, "fields": {"max_concurrency": "invalid"}}),
                "missing": ("delete", {"provider_id": "prov-missing"}),
            }[case]
            code, answer = await call(action, request)
            assert code == status and answer["error"]["message"] == message, answer
            code, answer = await call("list", {})
            assert code == 200 and any(p["provider_id"] == pid for p in answer["result"]["providers"])
            assert await pool.fetchval("SELECT count(*) FROM manager_actions") == 0
    run_db(run, monkeypatch, timeout=30)
    assert secret not in caplog.text
