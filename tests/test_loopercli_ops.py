"""Operator credentials are transient; portable configuration travels through OpenAPI."""

import io
import json
import sys

import pytest

from looper import loopercli
from looper.agents import api_v1
from tests.test_loopercli import Server, _send


@pytest.mark.parametrize("source", ["stdin", "file", "unsafe", "symlink"])
@pytest.mark.parametrize("action", ["set-password", "clear-password"])
def test_auth_operator_token_sources(tmp_path, monkeypatch, capsys, source, action):
    token = "operator-secret-without-client-prefix"
    path = tmp_path / "operator-token"
    path.write_text(token + "\n")
    path.chmod(0o644 if source == "unsafe" else 0o600)
    if source == "symlink":
        link = tmp_path / "link"
        link.symlink_to(path)
        path = link
    monkeypatch.setattr(sys, "stdin", io.StringIO(token + "\n"))
    passwords = iter(["new-password", "new-password"])
    monkeypatch.setattr(loopercli.getpass, "getpass", lambda prompt: next(passwords))
    def respond(handler, request):
        assert request[2]["authorization"] == f"Bearer {token}"
        _send(handler, 200, api_v1.document() if request[1].endswith("openapi.json")
              else {"status": "ok", "result": {"password_set": True, "user": "admin"}})
    server = Server(respond)
    try:
        client_file = tmp_path / "client.json"
        argv = ["--client-file", str(client_file), "auth", action, "--url", server.url]
        if source != "stdin":
            argv += ["--operator-token-file", str(path)]
        assert loopercli.main(argv) == (4 if source in ("unsafe", "symlink") else 0)
        output = capsys.readouterr()
        assert token not in output.out + output.err and "new-password" not in output.out + output.err
        assert token not in argv and not client_file.exists()
        if source in ("unsafe", "symlink"):
            assert "0600" in output.err and not server.requests
        else:
            assert server.requests[-1][1:] == ("/api/v1/auth/" + action.replace("-", "_"),
                server.requests[-1][2], {"user": "admin", "password": "new-password"} if action == "set-password" else {})
    finally:
        server.close()


def test_auth_mismatch_does_not_send_password(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(sys, "stdin", io.StringIO("operator-token\n"))
    passwords = iter(["first-password", "second-password"])
    monkeypatch.setattr(loopercli.getpass, "getpass", lambda prompt: next(passwords))
    server = Server(lambda handler, request: _send(handler, 200, api_v1.document()))
    try:
        assert loopercli.main(["auth", "set-password", "--url", server.url]) == 2
        assert not any(request[0] == "POST" for request in server.requests)
        output = capsys.readouterr()
        assert "passwords differ" in output.err and "first-password" not in output.err
    finally:
        server.close()


def test_auth_refuses_token_arguments_without_echoing_them(capsys):
    assert loopercli.main(["auth", "clear-password", "--token", "operator-secret"]) == 2
    captured = capsys.readouterr()
    assert "operator-secret" not in captured.out + captured.err


def test_auth_does_not_fall_back_to_echoed_password_input(monkeypatch, capsys):
    import warnings
    monkeypatch.setattr(sys, "stdin", io.StringIO("operator-token\n"))
    def unavailable(prompt):
        warnings.warn("cannot control terminal echo", loopercli.getpass.GetPassWarning)
        raise AssertionError("must stop before reading echoed input")
    monkeypatch.setattr(loopercli.getpass, "getpass", unavailable)
    assert loopercli.main(["auth", "set-password"]) == 2
    assert "echo disabled" in capsys.readouterr().err


def test_doctor_and_config_commands_use_openapi(tmp_path, monkeypatch, capsys):
    document = {"format": "looper-config/v3", "projects": []}
    def respond(handler, request):
        path = request[1]
        result = ({"document": document} if path.endswith("config/export") else
                  {"import_id": "cfgimp-test", "plan": {}} if path.endswith("import_preview") else
                  {"checks": [{"id": "pg_dump", "status": "warn"}]})
        _send(handler, 200, api_v1.document() if path.endswith("openapi.json") else {"status": "ok", "result": result})
    server = Server(respond)
    client = tmp_path / "client.json"
    client.write_text(json.dumps({"url": server.url, "token": "lpc_test"}))
    client.chmod(0o600)
    base = ["--client-file", str(client)]
    exported = tmp_path / "portable.json"
    try:
        assert loopercli.main([*base, "doctor"]) == 0
        assert loopercli.main([*base, "config", "export", str(exported), "--include-secrets"]) == 0
        assert json.loads(exported.read_text()) == document
        assert exported.stat().st_mode & 0o777 == 0o600
        assert loopercli.main([*base, "config", "import", str(exported)]) == 0
        assert server.requests[-1][3] == {"document": document}
        assert loopercli.main([*base, "config", "import", "--apply", "cfgimp-test"]) == 0
        assert server.requests[-1][3] == {"import_id": "cfgimp-test"}
    finally:
        server.close()
