"""Registry discovery, authorization and generated ordinary CLI operations."""

import asyncio
import json
import subprocess
import sys
from types import SimpleNamespace

import pytest

from looper import loopercli
from looper.agents import api_v1, operator_auth, profile_registry
from looper.agents.ops import OpError
from tests.test_api_v1_auth import CLIENT_TOKEN, _bearer, _send, _serving, clients as clients
from tests.test_operator_auth import PASSWORD, TOKEN, USER, _password_login


@pytest.mark.parametrize("identity", ["token", "cookie", "session"])
def test_operator_credentials_read_snapshot_with_configured_binary(tmp_path, monkeypatch, identity):
    calls = []

    async def view(*, server_claude):
        calls.append(server_claude)
        return {"providers": [{"env": [{"key": "EXAMPLE", "value": "synthetic-value"}]}]}

    monkeypatch.setattr(profile_registry, "registry_view", view)
    if identity == "session":
        operator_auth.set_password(tmp_path, USER, PASSWORD)

    async def run():
        async with _serving(tmp_path) as server:
            server._claude_bin = "configured-claude"
            headers = _bearer(TOKEN)
            if identity == "cookie":
                headers = {"Cookie": f"{operator_auth.cookie_name(server._port)}={TOKEN}"}
            elif identity == "session":
                status, fields, _ = await _password_login(server)
                assert status == 303
                headers = {"Cookie": fields["set-cookie"].split(";", 1)[0]}
            status, _, answer = await _send(server, "POST /api/v1/registry/view", headers, "{}")
            assert status == 200 and answer["result"]["providers"][0]["env"][0]["value"] == "synthetic-value"
            assert calls == ["configured-claude"]

    asyncio.run(run())


@pytest.mark.parametrize("body", ["{}", "not-json", '{"unexpected":"value"}'])
def test_clients_are_refused_before_snapshot_construction_or_body_validation(tmp_path, monkeypatch, clients, body):
    async def forbidden(**kwargs):
        pytest.fail("a client must never construct the full registry")

    monkeypatch.setattr(profile_registry, "registry_view", forbidden)

    async def run():
        async with _serving(tmp_path) as server:
            status, _, answer = await _send(server, "POST /api/v1/registry/view", _bearer(CLIENT_TOKEN), body)
            assert status == 403 and answer["error"]["code"] == "forbidden"

    asyncio.run(run())


@pytest.mark.parametrize("family,action,options,body", [
    ("registry", "view", [], {}),
    ("runtimes", "probe", ["--runtime-id", "rt-example"], {"runtime_id": "rt-example"}),
])
def test_document_generates_cli_requests(family, action, options, body, capsys, tmp_path):
    document = api_v1.document()
    args = loopercli.build_parser(document).parse_args([family, action, *options])
    calls = []
    api = SimpleNamespace(call=lambda *parts: (calls.append(parts) or (200, {"status": "ok", "result": {}})))
    assert args._run(args, SimpleNamespace(api=lambda: api, as_json=True, session="registry", operator=True)) == 0
    assert calls == [("POST", f"/api/v1/{family}/{action}", body)]
    # A fresh CLI process consumes only the document: server imports must remain unnecessary.
    path = tmp_path / "openapi.json"
    path.write_text(json.dumps(document))
    source = """import json,sys
from looper import loopercli
args = loopercli.build_parser(json.load(open(sys.argv[1]))).parse_args(sys.argv[2:])
assert callable(args._run)
assert not any(m.startswith(('looper.agents', 'asyncpg', 'httpx')) for m in sys.modules)
"""
    subprocess.run([sys.executable, "-c", source, str(path), family, action, *options], check=True, timeout=10)


@pytest.mark.parametrize("operation,body", [
    ("registry.view", {"extra": True}), ("runtimes.probe", {}),
    ("runtimes.probe", {"runtime_id": None}), ("runtimes.probe", {"runtime_id": 3}),
    ("runtimes.probe", {"runtime_id": "rt-example", "extra": True}),
])
def test_registry_requests_are_closed_and_identifiers_are_strings(operation, body):
    op = api_v1.find("POST", "/api/v1/" + operation.replace(".", "/"))
    with pytest.raises(OpError):
        api_v1.validate_arguments(op.request, body)


def test_registry_operations_preserve_discovery_tool_boundary():
    from looper.agents.v1_domains import registry

    assert [(op.operation_id, op.risk, op.operator_only, op.hand, op.confirms, op.tool)
            for op in registry.OPERATIONS] == [
        ("registry.view", "read", True, False, False, None),
        ("runtimes.probe", "write", False, False, False, None)]
    schemas = api_v1.document()["components"]["schemas"]
    for name in ("Registry", "Runtime", "RuntimeType", "Provider", "Model", "AgentProfile", "VendorType", "RegistryLimits"):
        assert schemas[name] == registry.SCHEMAS[name]
