"""API client credential operations are generated commands, never model-facing Tools."""

import asyncio

import pytest

from looper import loopercli
from looper.agents import api_v1
from tests.test_api_v1_auth import CLIENT_TOKEN, _bearer, _send, _serving, clients


@pytest.mark.parametrize(("action", "flags", "body"), [
    ("list", [], {}),
    ("create", ["--name", "Example agent"], {"name": "Example agent"}),
    ("revoke", ["--client-id", "client-0123456789ab"], {"client_id": "client-0123456789ab"}),
])
def test_generated_cli_request_and_credential_contract(action, flags, body):
    document = api_v1.document()
    generated = next(op for _, _, _, _, op in loopercli.generated_operations(document)
                     if op.path == f"/api/v1/clients/{action}")
    args = loopercli.build_parser(document).parse_args(["clients", action, *flags])
    assert generated.method == "POST" and loopercli.request_body(args, generated) == body
    operation = api_v1.find("POST", generated.path)
    assert operation.operator_only and operation.credential_write and not operation.hand
    assert operation.tool is None and not operation.confirms
    assert operation.request["additionalProperties"] is False
    if action == "create":
        token = operation.result["properties"]["token"]
        assert "response-only" in token["description"] and "writeOnly" not in token


@pytest.mark.parametrize("action", ["list", "create", "revoke"])
@pytest.mark.parametrize("body", ["{}", "not json", "[]", '{"password":"request-secret"}'])
def test_client_is_refused_before_parsing_service_or_audit(tmp_path, clients, monkeypatch, action, body):
    def refused(*args, **kwargs):
        pytest.fail("a client credential request reached parsing, service or audit")

    async def run():
        async with _serving(tmp_path, listen=True) as server:
            monkeypatch.setattr(server, "_v1_body", refused)
            monkeypatch.setattr(server, "_client_ops", None)
            status, _, answer = await _send(server, f"POST /api/v1/clients/{action}",
                                            _bearer(CLIENT_TOKEN), body)
            assert status == 403 and answer["error"]["code"] == "forbidden"
            assert clients.created == [] and clients.revoked == []
    asyncio.run(run())
