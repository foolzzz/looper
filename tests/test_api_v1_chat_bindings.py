"""Operator routing operations are closed v1 contracts and generated CLI commands."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from looper import loopercli
from looper.agents import api_v1
from looper.agents.ops import ErrorCode, OpError
from tests.test_api_v1_auth import CLIENT_TOKEN, _bearer, _send, _serving, clients  # noqa: F401
from tests.test_loopercli import Server, _send as cli_send, write_client_file
from tests.test_operator_auth import TOKEN


CASES = [
    ("manager_chat_bindings.list", {}, "read"),
    ("manager_chat_bindings.put", {"channel_id": "channel-telegram", "chat_id": "-10042",
                                   "manager_id": "manager"}, "write"),
    ("manager_chat_bindings.delete", {"channel_id": "channel-telegram", "chat_id": "-10042"}, "write"),
    ("channel_chats.list", {"channel_id": "channel-telegram"}, "read"),
    ("channel_chats.set_policy", {"channel_id": "channel-telegram", "bound_chats_only": False}, "write"),
]
BINDING = {"channel_id": "channel-telegram", "chat_id": "-10042", "manager_id": "manager",
           "created_at": "2026-10-25T12:00:00+00:00", "channel_type": "telegram", "bot_name": "Example bot",
           "chat_name": None, "manager_name": "Example Manager", "manager_enabled": False,
           "manager_deleted": False}
SEEN = {"chat_id": "-10042", "name": None, "count": None, "last_at": None, "binding": BINDING,
        "effective_manager_id": "manager", "effective_manager_name": "Example Manager",
        "effective_manager_enabled": False, "effective_manager_deleted": False}
UNBOUND = {"chat_id": "-10043", "name": None, "count": 1, "last_at": "2026-10-25T12:00:00+00:00"}
RESULTS = {
    "manager_chat_bindings.list": {"bindings": [BINDING]},
    "manager_chat_bindings.put": {"binding": BINDING},
    "manager_chat_bindings.delete": {"channel_id": "channel-telegram", "chat_id": "-10042", "deleted": False},
    "channel_chats.list": {"channel_id": "channel-telegram", "bot_manager_id": "manager",
                           "bound_chats_only": False, "chats": [SEEN], "unbound_chats": [UNBOUND],
                           "next_cursor": None, "has_more": False},
    "channel_chats.set_policy": {"channel_id": "channel-telegram", "bound_chats_only": False},
}


def _path(operation_id):
    return "/api/v1/" + operation_id.replace(".", "/")


def _operation(operation_id):
    operation = api_v1.find("POST", _path(operation_id))
    assert operation is not None, operation_id
    return operation


def _closed_objects(schema):
    if "object" in ([schema.get("type")] if isinstance(schema.get("type"), str) else schema.get("type", [])):
        assert schema.get("additionalProperties") is False
    for child in schema.get("properties", {}).values():
        _closed_objects(child)
    if "items" in schema:
        _closed_objects(schema["items"])
    for key in ("anyOf", "oneOf", "allOf"):
        for child in schema.get(key, []):
            _closed_objects(child)


@pytest.mark.parametrize("operation_id,body,risk", CASES)
def test_operation_is_discovered_with_closed_operator_contract(operation_id, body, risk):
    operation = _operation(operation_id)
    domain = api_v1.domain_for(operation)
    assert domain.__name__ == "looper.agents.v1_domains.chat_bindings"
    assert operation.operation_id == operation_id and operation.method == "POST"
    assert operation.operator_only and not operation.credential_write and not operation.hand
    assert operation.risk == risk and operation.tool is None and not operation.confirms
    _closed_objects(operation.request)
    _closed_objects(operation.result)
    api_v1.validate_arguments(operation.request, body)
    api_v1.validate_arguments(operation.result, RESULTS[operation_id])
    document = api_v1.document()["paths"][_path(operation_id).removeprefix("/api/v1")]["post"]
    assert document["operationId"] == operation_id
    result = document["responses"]["200"]["content"]["application/json"]["schema"]
    assert result["allOf"][1]["properties"]["result"] == operation.result


@pytest.mark.parametrize("operation_id,body,risk", CASES)
def test_result_fields_are_required_and_unknown_fields_are_rejected(operation_id, body, risk):
    schema = _operation(operation_id).result
    result = RESULTS[operation_id]
    assert set(schema["required"]) == set(result)
    with pytest.raises(OpError):
        api_v1.validate_arguments(schema, {**result, "unexpected": True})
    for key in result:
        with pytest.raises(OpError):
            api_v1.validate_arguments(schema, {name: value for name, value in result.items() if name != key})


@pytest.mark.parametrize("dto", ["binding", "seen", "unbound"])
def test_nested_dtos_require_all_fields_and_are_closed(dto):
    operation = _operation("channel_chats.list")
    chats = operation.result["properties"]["chats"]["items"]
    binding = chats["properties"]["binding"]
    if "anyOf" in binding:
        binding = next(variant for variant in binding["anyOf"] if variant.get("type") == "object")
    schema = {"binding": binding, "seen": chats,
              "unbound": operation.result["properties"]["unbound_chats"]["items"]}[dto]
    result = {"binding": BINDING, "seen": SEEN, "unbound": UNBOUND}[dto]
    api_v1.validate_arguments(schema, result)
    with pytest.raises(OpError):
        api_v1.validate_arguments(schema, {**result, "transcript": "must not be exposed"})
    for key in result:
        with pytest.raises(OpError):
            api_v1.validate_arguments(schema, {name: value for name, value in result.items() if name != key})


@pytest.mark.parametrize("operation_id,body,risk", CASES)
@pytest.mark.parametrize("raw", [None, "not json", "[]", '{"confirmation_id":"cnf-synthetic"}'])
@pytest.mark.usefixtures("clients")
def test_client_refused_before_parsing_service_or_audit(tmp_path, monkeypatch, operation_id, body, risk, raw):
    def refused(*args, **kwargs):
        pytest.fail("operator-only request reached parsing, service or audit")

    async def run():
        async with _serving(tmp_path) as server:
            monkeypatch.setattr(server, "_v1_body", refused)
            monkeypatch.setattr(server, "_call_v1", refused)
            monkeypatch.setattr(api_v1.domain_for(_operation(operation_id)), "run", refused)
            status, _, answer = await _send(server, "POST " + _path(operation_id), _bearer(CLIENT_TOKEN),
                                            json.dumps(body) if raw is None else raw)
            assert status == 403 and answer["error"]["code"] == "forbidden"
            assert server._client_ops is None

    asyncio.run(run())


@pytest.mark.parametrize("operation_id,body,risk", CASES)
def test_unauthenticated_request_refused_before_body_parsing(tmp_path, monkeypatch, operation_id, body, risk):
    def refused(*args, **kwargs):
        pytest.fail("unauthenticated request reached body parsing")

    async def run():
        async with _serving(tmp_path) as server:
            monkeypatch.setattr(server, "_v1_body", refused)
            status, _, answer = await _send(server, "POST " + _path(operation_id), body="not json")
            assert status == 401 and answer["error"]["code"] == "unauthorized"

    asyncio.run(run())


@pytest.mark.parametrize("operation_id,body,risk", CASES)
def test_operator_request_crosses_transport_to_domain(tmp_path, monkeypatch, operation_id, body, risk):
    operation = _operation(operation_id)
    domain = api_v1.domain_for(operation)
    service = AsyncMock(return_value=RESULTS[operation_id])
    monkeypatch.setattr(domain, "run", service)

    async def run():
        async with _serving(tmp_path) as server:
            # Use the ordinary operation callback, without requiring a database audit fixture.
            async def call(principal, session, name, data, *, tool, validator):
                return {"status": "ok", "result": await tool.run(SimpleNamespace(arguments=validator(tool.schema, data)))}

            server._client_ops = SimpleNamespace()
            monkeypatch.setattr(server, "_call_v1", call)
            status, headers, answer = await _send(server, "POST " + _path(operation_id), _bearer(TOKEN), json.dumps(body))
            assert status == 200 and answer == {"status": "ok", "result": RESULTS[operation_id]}
            assert headers["cache-control"] == "no-store"
            assert service.await_count == 1
            assert service.await_args.args[2:4] == (body, api_v1.OPERATOR)

    asyncio.run(run())


@pytest.mark.parametrize("operation_id,body", [
    ("manager_chat_bindings.list", {"channel_id": ""}),
    ("manager_chat_bindings.list", {"manager_id": None}),
    ("manager_chat_bindings.put", {"channel_id": "channel-telegram", "chat_id": "1"}),
    ("manager_chat_bindings.delete", {"channel_id": "channel-telegram", "chat_id": 42}),
    ("channel_chats.list", {"channel_id": "channel-telegram", "limit": 0}),
    ("channel_chats.list", {"channel_id": "channel-telegram", "limit": 201}),
    ("channel_chats.list", {"channel_id": "channel-telegram", "limit": True}),
    ("channel_chats.list", {"channel_id": "channel-telegram", "after": ""}),
    ("channel_chats.set_policy", {"channel_id": "channel-telegram", "bound_chats_only": "false"}),
    ("channel_chats.set_policy", {"channel_id": "channel-telegram", "bound_chats_only": 0}),
])
def test_invalid_declared_inputs_are_refused(operation_id, body):
    with pytest.raises(OpError) as error:
        api_v1.validate_arguments(_operation(operation_id).request, body)
    assert error.value.code is ErrorCode.INVALID


@pytest.mark.parametrize("operation_id,body,risk", [case for case in CASES if case[2] == "write"])
def test_mutation_without_live_registry_refuses_before_storage(monkeypatch, operation_id, body, risk):
    from looper.agents.channels import chat_routing
    from looper.db import connection

    def refused(*args, **kwargs):
        pytest.fail("mutation without registry reached storage")

    monkeypatch.setattr(connection, "get_pool", refused)
    for name in ("bind_chat", "unbind_chat", "set_chat_policy"):
        monkeypatch.setattr(chat_routing, name, refused)
    operation = _operation(operation_id)

    async def run():
        with pytest.raises(OpError) as error:
            await api_v1.domain_for(operation).run(SimpleNamespace(_channels=None), operation, body,
                                                  api_v1.OPERATOR, {})
        assert error.value.code is ErrorCode.UNAVAILABLE

    asyncio.run(run())


CLI_CASES = [
    ("manager_chat_bindings.list", ["--channel-id", "channel-telegram", "--manager-id", "manager"],
     {"channel_id": "channel-telegram", "manager_id": "manager"}),
    ("manager_chat_bindings.put", ["--channel-id", "channel-telegram", "--chat-id=-10042", "--manager-id", "manager"],
     CASES[1][1]),
    ("manager_chat_bindings.delete", ["--channel-id", "channel-telegram", "--chat-id=-10042"], CASES[2][1]),
    ("channel_chats.list", ["--channel-id", "channel-telegram", "--limit", "2", "--after", "-10041"],
     {"channel_id": "channel-telegram", "limit": 2, "after": "-10041"}),
    ("channel_chats.set_policy", ["--channel-id", "channel-telegram", "--no-bound-chats-only"], CASES[4][1]),
]


@pytest.mark.parametrize("operation_id,flags,body", CLI_CASES)
def test_generated_cli_uses_transient_operator_auth_and_declared_request(tmp_path, capsys, operation_id, flags, body):
    def respond(handler, request):
        assert request[2]["authorization"] == "Bearer " + TOKEN
        cli_send(handler, 200, api_v1.document() if request[1].endswith("openapi.json")
                 else {"status": "ok", "result": RESULTS[operation_id]})

    server = Server(respond)
    try:
        token_file = tmp_path / "operator-token"
        token_file.write_text(TOKEN + "\n")
        token_file.chmod(0o600)
        client_file = tmp_path / "missing-client.json"
        group, action = operation_id.replace("_", "-").split(".")
        assert group not in loopercli.HAND_COMMANDS
        argv = ["--client-file", str(client_file), "--json", group, action, *flags,
                "--url", server.url, "--operator-token-file", str(token_file)]
        assert loopercli.main(argv) == 0
        assert server.requests[-1][0:2] == ("POST", _path(operation_id))
        assert server.requests[-1][3] == body
        assert not client_file.exists()
        output = capsys.readouterr()
        assert json.loads(output.out) == {"status": "ok", "result": RESULTS[operation_id]}
        assert TOKEN not in output.out + output.err
    finally:
        server.close()


@pytest.mark.parametrize("operation_id,flags,body", CLI_CASES)
def test_generated_cli_help_advertises_operator_options(operation_id, flags, body, capsys):
    group, action = operation_id.replace("_", "-").split(".")
    with pytest.raises(SystemExit) as exit:
        loopercli.build_parser(api_v1.document()).parse_args([group, action, "--help"])
    assert exit.value.code == 0
    output = capsys.readouterr().out
    assert "--url" in output and "--operator-token-file" in output


@pytest.mark.parametrize("operation_id,flags,body", CLI_CASES)
def test_client_file_cannot_authorize_generated_operator_command(tmp_path, capsys, operation_id, flags, body):
    server = Server(lambda handler, request: cli_send(handler, 200, api_v1.document()
                    if request[1].endswith("openapi.json") else {"status": "error", "error": {
                        "code": "forbidden", "message": "operator identity is required"}}))
    try:
        client_file = write_client_file(tmp_path / "client.json", server.url)
        group, action = operation_id.replace("_", "-").split(".")
        assert loopercli.main(["--client-file", str(client_file), group, action, *flags]) != 0
        assert not any(request[0] == "POST" for request in server.requests)
        output = capsys.readouterr()
        assert "operator" in output.err.lower()
    finally:
        server.close()


def test_generated_operator_bootstrap_preserves_a_business_url_field(tmp_path, capsys):
    def respond(handler, request):
        assert request[2]["authorization"] == "Bearer " + TOKEN
        cli_send(handler, 200, api_v1.document() if request[1].endswith("openapi.json")
                 else {"status": "ok", "result": {}})

    server = Server(respond)
    try:
        token = tmp_path / "operator-token"
        token.write_text(TOKEN + "\n")
        token.chmod(0o600)
        assert loopercli.main(["--client-file", str(tmp_path / "missing-client.json"), "catalog-sources", "create-mcp",
                               "--name", "example", "--transport", "http", "--command", "example",
                               "--url", "https://example.test/mcp", "--server-url", server.url,
                               "--operator-token-file", str(token)]) == 0
        assert server.requests[-1][0:2] == ("POST", "/api/v1/catalog_sources/create_mcp")
        assert server.requests[-1][3] == {"name": "example", "transport": "http", "command": "example",
                                          "url": "https://example.test/mcp"}
        assert TOKEN not in capsys.readouterr().out
    finally:
        server.close()
