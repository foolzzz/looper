"""Credential contracts through real HTTP and a disposable PostgreSQL schema."""

import asyncio
import io
import json
import logging
import sys
from contextlib import asynccontextmanager

import pytest

from looper import loopercli
from looper.agents import api_v1, operator_auth
from looper.agents.bus import MessageBus
from looper.agents.ops.clients import ClientOps
from looper.agents.ops.service import Platform
from looper.db import api_clients
from tests.db_support import DSN, run_db
from tests.test_api_v1_auth import _bearer, _send, _serving
from tests.test_operator_auth import TOKEN

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")


@pytest.mark.parametrize("source", ["stdin", "file"])
def test_cli_manages_clients_with_transient_operator_auth(tmp_path, monkeypatch, capsys, source):
    async def run(pool):
        async with _api(tmp_path) as (server, call):
            url = f"http://127.0.0.1:{server._port}"
            token = (await call("create", {"name": "CLI login"}))[2]["result"]["token"]
            client_file = tmp_path / "client.json"
            base = ["--client-file", str(client_file), "--json"]
            monkeypatch.setattr(sys, "stdin", io.StringIO(token + "\n"))
            assert await asyncio.to_thread(loopercli.main, [*base, "login", "--url", url]) == 0
            capsys.readouterr()
            saved = client_file.read_bytes()
            operator_file = tmp_path / "operator-token"
            operator_file.write_text(TOKEN + "\n")
            operator_file.chmod(0o600)

            async def cli(action, *flags):
                monkeypatch.setattr(sys, "stdin", io.StringIO(TOKEN + "\n"))
                argv = [*base, "clients", action, "--url", url, *flags]
                if source == "file":
                    argv += ["--operator-token-file", str(operator_file)]
                assert await asyncio.to_thread(loopercli.main, argv) == 0
                output = capsys.readouterr()
                assert output.err == "" and TOKEN not in output.out
                assert client_file.read_bytes() == saved
                return json.loads(output.out)["result"]

            created = await cli("create", "--name", "Managed by CLI")
            client_id = created["client"]["id"]
            assert (await pool.fetchrow("SELECT name FROM api_clients WHERE id=$1", client_id))["name"] == "Managed by CLI"
            assert (await _send(server, "GET /api/v1/whoami", _bearer(created["token"])))[0] == 200
            listed = await cli("list")
            assert client_id in {client["id"] for client in listed["clients"]}
            assert created["token"] not in json.dumps(listed)
            assert await cli("revoke", "--client-id", client_id) == {"revoked": client_id}
            assert (await _send(server, "GET /api/v1/whoami", _bearer(created["token"])))[0] == 401
            assert client_id not in {client["id"] for client in (await cli("list"))["clients"]}
            assert await pool.fetchval("SELECT revoked_at IS NOT NULL FROM api_clients WHERE id=$1", client_id)
    run_db(run, monkeypatch, timeout=30)


@asynccontextmanager
async def _api(tmp_path, *, identity="token"):
    operator_auth.set_password(tmp_path, "admin", "synthetic-password")
    async with _serving(tmp_path, listen=True) as server:
        server._client_ops = ClientOps(Platform(bus=MessageBus(runtime_root=tmp_path),
                                               task_router=server._task_router, runtime_root=tmp_path))
        headers = _bearer(TOKEN)
        if identity == "cookie":
            headers = {"Cookie": f"{operator_auth.cookie_name(server._port)}={TOKEN}"}
        elif identity == "session":
            record = operator_auth.load_password(tmp_path)
            session = server._sessions.create(record.user, record.hash)
            headers = {"Cookie": f"{server._session_cookie_name()}={session}"}

        async def call(action, body=None):
            path = f"/api/v1/clients/{action}"
            status, fields, answer = await _send(server, f"POST {path}", headers,
                body if isinstance(body, str) else json.dumps(body if body is not None else {}))
            if status == 200:
                api_v1.validate_arguments({"type": "object", "properties": {
                    "result": api_v1.find("POST", path).result}}, {"result": answer["result"]})
            return status, fields, answer
        yield server, call


@pytest.mark.parametrize("identity", ["token", "cookie", "session"])
def test_operator_create_list_and_revoke_with_one_time_secret(tmp_path, monkeypatch, identity):
    async def run(pool):
        async with _api(tmp_path, identity=identity) as (server, call):
            status, headers, created = await call("create", {"name": " Example agent "})
            assert status == 200 and headers["cache-control"] == "no-store"
            client, token = created["result"]["client"], created["result"]["token"]
            assert set(client) == {"id", "name", "created_at", "last_used_at"}
            assert client["name"] == "Example agent" and client["last_used_at"] is None
            assert (await call("list"))[2]["result"] == {"clients": [client]}
            assert token not in json.dumps(client) and "sha256" not in json.dumps(client)
            row = await pool.fetchrow("SELECT * FROM api_clients WHERE id=$1", client["id"])
            assert row["token_sha256"] == api_clients.token_hash(token)
            assert token not in str(dict(row))
            assert (await _send(server, "GET /api/v1/whoami", _bearer(token)))[0] == 200
            listed = (await call("list"))[2]["result"]
            assert listed["clients"][0]["last_used_at"] is not None
            assert token not in json.dumps(listed) and "sha256" not in json.dumps(listed)
            assert (await call("revoke", {"client_id": client["id"]}))[2]["result"] == {"revoked": client["id"]}
            assert (await _send(server, "GET /api/v1/whoami", _bearer(token)))[0] == 401
            assert (await call("list"))[2]["result"] == {"clients": []}
            assert await pool.fetchval("SELECT revoked_at IS NOT NULL FROM api_clients WHERE id=$1", client["id"])
            assert (await call("create", {"name": "EXAMPLE AGENT"}))[0] == 200
    run_db(run, monkeypatch, timeout=30)


@pytest.mark.parametrize(("body", "expected", "code"), [
    ({"name": " "}, 400, "invalid"), ({"name": "a" * 41}, 400, "invalid"),
    ({"name": "-invalid"}, 400, "invalid"), ({"name": "a\nb"}, 400, "invalid"),
    ({"name": 3}, 400, "invalid"), ({"name": None}, 400, "invalid"),
    ({"name": "EXAMPLE"}, 409, "conflict"),
    ({"name": "new", "token": "request-secret"}, 400, "invalid"),
    ("[]", 400, "invalid"), ("not json", 400, "invalid"),
])
def test_invalid_names_and_duplicate_active_name_are_typed_and_atomic(tmp_path, monkeypatch, body, expected, code):
    async def run(pool):
        async with _api(tmp_path) as (_, call):
            assert (await call("create", {"name": "Example"}))[0] == 200
            before = await pool.fetch("SELECT * FROM api_clients")
            status, _, answer = await call("create", body)
            assert status == expected and answer["error"]["code"] == code
            assert await pool.fetch("SELECT * FROM api_clients") == before
    run_db(run, monkeypatch, timeout=30)


def test_normalized_name_limit_keeps_existing_whitespace_semantics(tmp_path, monkeypatch):
    async def run(pool):
        async with _api(tmp_path) as (_, call):
            status, _, answer = await call("create", {"name": " \n" + "a" * 40 + "\t "})
            assert status == 200 and answer["result"]["client"]["name"] == "a" * 40
    run_db(run, monkeypatch, timeout=30)


@pytest.mark.parametrize(("action", "body"), [
    ("list", {"unexpected": "request-secret"}),
    ("revoke", {"client_id": 3}),
    ("revoke", {"client_id": "client-000000000000", "unexpected": "request-secret"}),
])
def test_operator_requests_enforce_closed_objects_and_identifier_type(tmp_path, monkeypatch, action, body):
    async def run(pool):
        async with _api(tmp_path) as (_, call):
            assert (await call("create", {"name": "Example"}))[0] == 200
            before = await pool.fetch("SELECT * FROM api_clients")
            status, _, answer = await call(action, body)
            assert status == 400 and answer["error"]["code"] == "invalid"
            assert "request-secret" not in json.dumps(answer)
            assert await pool.fetch("SELECT * FROM api_clients") == before
    run_db(run, monkeypatch, timeout=30)


@pytest.mark.parametrize("already_revoked", [False, True], ids=["unknown", "already-revoked"])
def test_revoke_missing_active_client_returns_404(tmp_path, monkeypatch, already_revoked):
    async def run(pool):
        async with _api(tmp_path) as (_, call):
            client_id = "client-000000000000"
            if already_revoked:
                client_id = (await call("create", {"name": "Example"}))[2]["result"]["client"]["id"]
                assert (await call("revoke", {"client_id": client_id}))[0] == 200
            status, _, answer = await call("revoke", {"client_id": client_id})
            assert status == 404 and answer["error"]["code"] == "not_found"
    run_db(run, monkeypatch, timeout=30)


@pytest.mark.parametrize(("method", "action"), [("GET", "list"), ("POST", "create"), ("DELETE", "revoke")])
def test_legacy_method_is_404_with_positive_v1_control(tmp_path, monkeypatch, method, action):
    async def run(pool):
        async with _api(tmp_path) as (server, call):
            created = (await call("create", {"name": "Example"}))[2]["result"]
            client_id = created["client"]["id"]
            body = {"client_id": client_id} if action == "revoke" else {"name": "Other"} if action == "create" else {}
            assert (await call(action, body))[0] == 200
            path = f"/api/clients/{client_id}" if method == "DELETE" else "/api/clients"
            assert (await _send(server, f"{method} {path}", _bearer(TOKEN), json.dumps(body)))[0] == 404
    run_db(run, monkeypatch, timeout=30)


def test_success_failure_logs_and_audit_exclude_credential_requests(tmp_path, monkeypatch, caplog):
    async def run(pool):
        async with _api(tmp_path) as (server, call):
            # Positive audit control: an invalid ordinary write is still recorded.
            assert (await _send(server, "POST /api/v1/ops/task_update", _bearer(TOKEN), "{}"))[0] == 400
            before = await pool.fetch("SELECT * FROM manager_actions")
            assert len(before) == 1 and before[0]["tool"] == "looper_task_update"
            created = (await call("create", {"name": "request-secret"}))[2]["result"]
            token = created["token"]
            assert (await call("create", {"name": "request-secret"}))[0] == 409
            assert (await call("create", {"name": "new", "password": "body-secret"}))[0] == 400
            for action in ("list", "create", "revoke"):
                status, _, answer = await _send(server, f"POST /api/v1/clients/{action}", _bearer(token), "not json")
                assert status == 403 and "confirmation_id" not in answer
            assert not server._client_ops._pending
            status, _, answer = await _send(server, "POST /api/v1/confirm", _bearer(token),
                                            '{"confirmation_id":"clients.create"}')
            assert status == 404 and answer["error"]["code"] == "not_found"
            assert (await call("list"))[0] == 200
            assert (await call("revoke", {"client_id": created["client"]["id"]}))[0] == 200

            async def failing_create(name):
                raise RuntimeError(f"body-secret {token}")
            monkeypatch.setattr(api_clients, "create", failing_create)
            status, _, answer = await call("create", {"name": "new"})
            assert status == 503 and answer["error"]["code"] == "unavailable"
            rows = await pool.fetch("SELECT * FROM manager_actions")
            assert rows == before
            for secret in (token, "body-secret", "request-secret", TOKEN):
                assert secret not in caplog.text + str(rows) + json.dumps(answer)
            assert "op=clients.create status=ok" in caplog.text
            assert "op=clients.create status=error:conflict" in caplog.text
            assert "op=clients.create status=error:unavailable" in caplog.text
    with caplog.at_level(logging.DEBUG):
        run_db(run, monkeypatch, timeout=30)
