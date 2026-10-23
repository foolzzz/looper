"""Online operator commands use v1 without giving API clients operator credentials."""

import asyncio
import json
from pathlib import Path

import pytest

from looper.agents import api_v1, operator_auth, self_check
from tests.test_api_v1_auth import CLIENT_TOKEN, _bearer, _send, _serving, clients
from tests.test_operator_auth import TOKEN


@pytest.mark.parametrize("action", ["set_password", "clear_password"])
def test_password_operations_require_operator(tmp_path, clients, action, caplog):
    async def run():
        operator_auth.set_password(tmp_path, "admin", "original-password")
        path = tmp_path / operator_auth.PASSWORD_FILE
        before = path.read_bytes()
        async with _serving(tmp_path) as server:
            body = {"password": "replacement-password", "user": "admin"} if action == "set_password" else {}
            status, _, answer = await _send(server, f"POST /api/v1/auth/{action}",
                                           _bearer(CLIENT_TOKEN), json.dumps(body))
            assert status == 403 and answer["error"]["code"] == "forbidden"
            assert path.read_bytes() == before
            status, _, answer = await _send(server, f"POST /api/v1/auth/{action}",
                                           _bearer(TOKEN), json.dumps(body))
            assert status == 200 and answer["status"] == "ok"
            if action == "set_password":
                assert operator_auth.verify(operator_auth.load_password(tmp_path), "admin", body["password"])
                assert body["password"] not in path.read_text()
            else:
                assert not path.exists()
            assert "replacement-password" not in json.dumps(answer) + caplog.text
    asyncio.run(run())


@pytest.mark.parametrize("identity", ["cookie", "session"])
@pytest.mark.parametrize("action", ["set_password", "clear_password"])
def test_password_operator_cookie_and_session_clear_sessions(tmp_path, clients, identity, action):
    async def run():
        operator_auth.set_password(tmp_path, "admin", "original-password")
        record = operator_auth.load_password(tmp_path)
        async with _serving(tmp_path) as server:
            session = server._sessions.create(record.user, record.hash)
            headers = {"Cookie": (f"{operator_auth.cookie_name(server._port)}={TOKEN}" if identity == "cookie"
                                  else f"{server._session_cookie_name()}={session}")}
            status, _, answer = await _send(server, f"POST /api/v1/auth/{action}", headers,
                                           json.dumps({"password": "replacement-password"} if action == "set_password"
                                                      else {}))
            assert status == 200
            assert not server._sessions.valid(session, record.hash)
    asyncio.run(run())


def test_system_check_returns_live_typed_checks(tmp_path, clients, monkeypatch):
    async def checks(ops, **kwargs):
        assert ops is settings and kwargs == {"claude_bin": "claude"}
        return [self_check.Check("pg_dump", "pg_dump", "warn", "missing", "install pg_dump")]

    settings = object()
    monkeypatch.setattr(self_check, "run_checks", checks)
    async def run():
        async with _serving(tmp_path) as server:
            server._ops_config = settings
            status, _, answer = await _send(server, "POST /api/v1/system/check", _bearer(CLIENT_TOKEN), "{}")
            assert status == 200
            assert answer["result"] == {"checks": [{"id": "pg_dump", "label": "pg_dump", "status": "warn",
                                                     "detail": "missing", "fix": "install pg_dump"}]}
    asyncio.run(run())


def test_online_commands_have_typed_results_in_the_one_document():
    ids = {op.operation_id: op for op in api_v1.operations()}
    for name in ("auth.set_password", "auth.clear_password", "system.check", "config.export",
                 "config.import_preview", "config.import_apply"):
        assert name in ids
        assert ids[name].result["properties"]
        assert not ids[name].result.get("x-looper-result-typed") is False
    assert ids["config.import_apply"].confirms


def test_password_keeps_raw_whitespace_and_errors_never_echo_values(tmp_path, clients, caplog):
    async def run():
        async with _serving(tmp_path) as server:
            password = "       A"
            status, _, answer = await _send(server, "POST /api/v1/auth/set_password", _bearer(TOKEN),
                                           json.dumps({"password": password}))
            assert status == 200
            assert operator_auth.verify(operator_auth.load_password(tmp_path), "admin", password)
            before = (tmp_path / operator_auth.PASSWORD_FILE).read_bytes()
            for body in ({"password": "short"}, {"password": None}, {"user": None, "password": "long-password"},
                         {"password": "long-password", "secret-unknown-key": "value"}):
                status, _, answer = await _send(server, "POST /api/v1/auth/set_password", _bearer(TOKEN),
                                               json.dumps(body))
                assert status == 400
                assert "long-password" not in json.dumps(answer) + caplog.text
                assert "secret-unknown-key" not in json.dumps(answer) + caplog.text
                assert (tmp_path / operator_auth.PASSWORD_FILE).read_bytes() == before
    asyncio.run(run())
