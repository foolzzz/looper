"""``/api/v1`` lets an API client in on loopback (and the operator), and nothing else: a client token is no credential
anywhere else, and no route manages credentials with it."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from looper.agents import operator_auth
from looper.agents.http_server import AgentHTTPServer
from looper.db import api_clients
from tests.test_operator_auth import NAVIGATE, TOKEN, ManagerOps, _exchange, _request

CLIENT_TOKEN = "lpc_" + "c" * 43
CLIENT = {"id": "client-0123456789ab", "name": "my agent", "last_used_at": None}


class _Writer:
    def __init__(self, peer: str) -> None:
        self.peer = peer
        self.data = bytearray()

    def write(self, data: bytes) -> None:
        self.data.extend(data)

    def get_extra_info(self, name: str) -> tuple[str, int]:
        return (self.peer, 12345)

    def close(self) -> None:
        pass

    async def drain(self) -> None:
        pass

    async def wait_closed(self) -> None:
        pass


@asynccontextmanager
async def _serving(tmp_path: Path, *, host: str = "127.0.0.1", listen: bool = False) -> AsyncIterator[AgentHTTPServer]:
    (tmp_path / "index.html").write_text("<html>dashboard</html>")
    server = AgentHTTPServer(
        host=host, port=0, operator_token=TOKEN, static_dir=tmp_path, runtime_root=tmp_path,
        web_adapter=SimpleNamespace(), task_router=SimpleNamespace(), supervisor=SimpleNamespace(),
        manager_ops=ManagerOps())
    if listen:
        await server.start()
    try:
        yield server
    finally:
        if listen:
            await server.stop()


async def _send_raw(server: AgentHTTPServer, line: str, headers: dict[str, str] | None = None, body: str = "", *,
                    peer: str = "127.0.0.1") -> tuple[int, dict[str, str], str]:
    request = _request(server._port, line, headers, body)
    if server._server is not None:
        return await _exchange(server._port, request)
    reader = asyncio.StreamReader()
    reader.feed_data(request.encode())
    reader.feed_eof()
    writer = _Writer(peer)
    await server._handle_connection(reader, writer)
    head, _, payload = writer.data.decode().partition("\r\n\r\n")
    lines = head.split("\r\n")
    fields = {key.lower(): value for key, value in (field.split(": ", 1) for field in lines[1:])}
    return int(lines[0].split()[1]), fields, payload


async def _send(server: AgentHTTPServer, line: str, headers: dict[str, str] | None = None, body: str = "", *,
                peer: str = "127.0.0.1") -> tuple[int, dict[str, str], Any]:
    status, fields, payload = await _send_raw(server, line, headers, body, peer=peer)
    return status, fields, json.loads(payload) if payload else None


@pytest.fixture
def clients(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    calls = SimpleNamespace(touched=[], created=[], revoked=[], found=[])

    async def find(token: str, **_: Any) -> dict[str, Any] | None:
        calls.found.append(token)
        return CLIENT if token == CLIENT_TOKEN else None

    async def touch(client_id: str, **_: Any) -> None:
        calls.touched.append(client_id)

    async def create(name: str, **_: Any) -> Any:
        calls.created.append(name)
        raise AssertionError("a client token must not create a client")

    async def revoke(client_id: str, **_: Any) -> bool:
        calls.revoked.append(client_id)
        raise AssertionError("a client token must not revoke a client")

    monkeypatch.setattr(api_clients, "find", find)
    monkeypatch.setattr(api_clients, "touch", touch)
    monkeypatch.setattr(api_clients, "create", create)
    monkeypatch.setattr(api_clients, "revoke", revoke)
    return calls


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def test_whoami_for_the_operator(tmp_path: Path, clients: SimpleNamespace) -> None:
    async def run() -> None:
        async with _serving(tmp_path) as server:
            status, headers, body = await _send(server, "GET /api/v1/whoami", _bearer(TOKEN))
            assert status == 200 and headers["cache-control"] == "no-store"
            assert body["status"] == "ok"
            assert body["result"]["principal"] == "operator" and body["result"]["client"] is None
            assert body["result"]["api_version"] == 1
            assert clients.found == [] and clients.touched == []

    asyncio.run(run())


def test_whoami_for_a_client_on_loopback(tmp_path: Path, clients: SimpleNamespace) -> None:
    async def run() -> None:
        async with _serving(tmp_path, listen=True) as server:
            status, _, body = await _send(server, "GET /api/v1/whoami", _bearer(CLIENT_TOKEN))
            assert status == 200
            assert body["result"]["principal"] == "client"
            assert body["result"]["client"] == {"id": CLIENT["id"], "name": "my agent"}
            assert clients.touched == [CLIENT["id"]]

    asyncio.run(run())


@pytest.mark.parametrize(("headers", "peer", "host"), [
    ({"Authorization": "Bearer lpc_unknown"}, "127.0.0.1", "127.0.0.1"),
    ({}, "127.0.0.1", "127.0.0.1"),
    ({"Authorization": f"Bearer {CLIENT_TOKEN}", "Host": "example.com"}, "127.0.0.1", "0.0.0.0"),
    ({"Authorization": f"Bearer {CLIENT_TOKEN}", "X-Forwarded-For": "10.0.0.1"}, "127.0.0.1", "127.0.0.1"),
    ({"Authorization": f"Bearer {CLIENT_TOKEN}", "Forwarded": "for=10.0.0.1"}, "127.0.0.1", "127.0.0.1"),
    ({"Authorization": f"Bearer {CLIENT_TOKEN}"}, "192.168.1.20", "127.0.0.1"),
    ({"Cookie": f"{operator_auth.cookie_name(0)}={CLIENT_TOKEN}"}, "127.0.0.1", "127.0.0.1"),
])
def test_refusals_are_unauthorized_and_never_log_the_token(
        tmp_path: Path, clients: SimpleNamespace, caplog: pytest.LogCaptureFixture,
        headers: dict[str, str], peer: str, host: str) -> None:
    async def run() -> None:
        async with _serving(tmp_path, host=host) as server:
            with caplog.at_level(logging.DEBUG):
                status, fields, body = await _send(server, "GET /api/v1/whoami", headers, peer=peer)
            assert status == 401
            assert body["status"] == "error" and body["error"]["code"] == "unauthorized"
            assert "Settings → API clients" in body["error"]["message"]
            assert fields["www-authenticate"] == 'Bearer realm="looper"'
            assert clients.touched == []

    asyncio.run(run())
    assert CLIENT_TOKEN not in caplog.text and "lpc_unknown" not in caplog.text
    assert "refused GET /api/v1/whoami" in caplog.text


def test_a_failing_lookup_is_unavailable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    async def find(token: str, **_: Any) -> None:
        raise ConnectionError("database down")

    monkeypatch.setattr(api_clients, "find", find)

    async def run() -> None:
        async with _serving(tmp_path) as server:
            status, _, body = await _send(server, "GET /api/v1/whoami", _bearer(CLIENT_TOKEN))
            assert status == 503 and body["error"]["code"] == "unavailable"

    asyncio.run(run())


def test_a_failing_touch_is_ignored(tmp_path: Path, clients: SimpleNamespace, monkeypatch: pytest.MonkeyPatch) -> None:
    async def touch(client_id: str, **_: Any) -> None:
        raise ConnectionError("database down")

    monkeypatch.setattr(api_clients, "touch", touch)

    async def run() -> None:
        async with _serving(tmp_path) as server:
            status, _, body = await _send(server, "GET /api/v1/whoami", _bearer(CLIENT_TOKEN))
            assert status == 200 and body["result"]["principal"] == "client"

    asyncio.run(run())


def test_unknown_v1_path_is_not_found(tmp_path: Path, clients: SimpleNamespace) -> None:
    async def run() -> None:
        async with _serving(tmp_path) as server:
            status, _, body = await _send(server, "GET /api/v1/nope", _bearer(CLIENT_TOKEN))
            assert status == 404 and body["error"] == {"code": "not_found", "message": "no GET /api/v1/nope"}
            # A path that exists answers another method with 405.
            status, _, body = await _send(server, "OPTIONS /api/v1/whoami", _bearer(CLIENT_TOKEN))
            assert status == 405 and body["error"] == {"code": "invalid", "message": "use GET"}
            status, _, body = await _send(server, "POST /api/v1/whoami", _bearer(CLIENT_TOKEN), "{}")
            assert status == 405

    asyncio.run(run())


def test_a_body_is_not_read_without_a_credential(tmp_path: Path, clients: SimpleNamespace) -> None:
    async def run() -> None:
        async with _serving(tmp_path) as server:
            status, _, body = await _send(server, "POST /api/v1/whoami", {"Content-Length": "1000000"})
            assert status == 401 and body["error"]["code"] == "unauthorized"

    asyncio.run(run())


@pytest.mark.parametrize(("line", "body"), [
    ("GET /api/tasks", ""),
    ("GET /api/clients", ""),
    ("POST /api/clients", '{"name": "x"}'),
    ("DELETE /api/clients/client-000000000000", ""),
])
def test_a_client_token_is_not_the_operator(tmp_path: Path, clients: SimpleNamespace, line: str, body: str) -> None:
    async def run() -> None:
        async with _serving(tmp_path) as server:
            status, _, _ = await _send_raw(server, line, _bearer(CLIENT_TOKEN), body)
            assert status == 401
            assert clients.created == [] and clients.revoked == [] and clients.found == []

    asyncio.run(run())


def test_a_client_token_does_not_sign_in(tmp_path: Path, clients: SimpleNamespace) -> None:
    async def run() -> None:
        async with _serving(tmp_path) as server:
            origin = f"http://127.0.0.1:{server._port}"
            form = {**_bearer(CLIENT_TOKEN), **NAVIGATE, "Origin": origin,
                    "Content-Type": "application/x-www-form-urlencoded"}
            for line, headers, body in (("POST /login", form, "code="), ("GET /login", _bearer(CLIENT_TOKEN), "")):
                status, fields, _ = await _send_raw(server, line, headers, body)
                assert "set-cookie" not in fields, line
                assert status != 303, line
            cookie = {"Cookie": f"{operator_auth.cookie_name(server._port)}={CLIENT_TOKEN}"}
            status, _, _ = await _send_raw(server, "GET /api/tasks", cookie)
            assert status == 401
            assert clients.found == []

    asyncio.run(run())
