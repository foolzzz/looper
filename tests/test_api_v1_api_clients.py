"""API client credential commands use transient operator authentication, never model-facing Tools."""

import asyncio
import io
import json
import sys

import pytest

from looper import loopercli
from looper.agents import api_v1
from looper.db import api_clients
from tests.test_api_v1_auth import CLIENT_TOKEN, _bearer, _send, _serving, clients
from tests.test_operator_auth import TOKEN


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


@pytest.mark.parametrize("source", ["stdin", "terminal"])
def test_cli_operator_can_list_without_a_client_file(tmp_path, monkeypatch, capsys, source):
    async def list_active():
        return []

    monkeypatch.setattr(api_clients, "list_active", list_active)
    monkeypatch.setattr(sys, "stdin", io.StringIO(TOKEN + "\n"))
    if source == "terminal":
        monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
        monkeypatch.setattr(loopercli.getpass, "getpass", lambda prompt: TOKEN)

    async def run():
        async with _serving(tmp_path, listen=True) as server:
            client_file = tmp_path / "missing-client.json"
            assert await asyncio.to_thread(loopercli.main, [
                "--client-file", str(client_file), "--json", "clients", "list",
                "--url", f"http://127.0.0.1:{server._port}"]) == 0
            output = capsys.readouterr()
            assert json.loads(output.out)["result"] == {"clients": []}
            assert not client_file.exists() and TOKEN not in output.out + output.err
    asyncio.run(asyncio.wait_for(run(), 10))


@pytest.mark.parametrize("source", ["empty", "client", "whitespace", "encoding", "unsafe", "symlink"])
def test_cli_clients_refuses_invalid_operator_credentials_before_http(tmp_path, monkeypatch, capsys, source):
    token = CLIENT_TOKEN if source == "client" else "" if source == "empty" else TOKEN
    if source == "whitespace":
        token += " invalid"
    path = tmp_path / "operator-token"
    path.write_bytes(b"\xff" if source == "encoding" else (token + "\n").encode())
    path.chmod(0o644 if source == "unsafe" else 0o600)
    if source == "symlink":
        link = tmp_path / "link"
        link.symlink_to(path)
        path = link

    def refused(*args, **kwargs):
        pytest.fail("invalid operator credentials must not reach HTTP")

    monkeypatch.setattr(loopercli.Api, "_open", refused)
    file_error = source in ("unsafe", "symlink", "encoding")
    assert loopercli.main(["--client-file", str(tmp_path / "missing-client.json"), "clients", "list",
                           "--operator-token-file", str(path)]) == (4 if file_error else 2)
    output = capsys.readouterr()
    expected = "UTF-8" if source == "encoding" else "0600" if file_error else "operator token is required"
    assert expected in output.err
    assert TOKEN not in output.out + output.err and CLIENT_TOKEN not in output.out + output.err


def test_cli_clients_refuses_token_arguments_without_echoing_them(tmp_path, capsys):
    assert loopercli.main(["--client-file", str(tmp_path / "missing-client.json"),
                           "clients", "list", "--token", TOKEN]) == 2
    output = capsys.readouterr()
    assert TOKEN not in output.out + output.err
