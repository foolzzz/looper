"""The operator's API credential does not grant access to agent tools, or reach agents."""

from __future__ import annotations

import asyncio
import json
import os
import re
import stat
import subprocess
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

from looper.agents import operator_auth
from looper.agents.http_server import AgentHTTPServer

TOKEN = "operator_" + "a" * 32
AGENT = "agent_" + "b" * 32
NAVIGATE = {"Sec-Fetch-Mode": "navigate", "Sec-Fetch-Dest": "document"}


class ManagerOps:
    def authorized(self, header: str | None) -> bool:
        return header == f"Bearer {AGENT}"

    def refuse(self, path: str) -> None:
        pass

    def listing(self) -> list:
        return []


@asynccontextmanager
async def _server(tmp_path: Path, token: str | None = TOKEN) -> AsyncIterator[int]:
    (tmp_path / "index.html").write_text("<html>dashboard</html>")
    (tmp_path / "assets").mkdir(exist_ok=True)
    (tmp_path / "assets" / "app.js").write_text("console.log('dashboard');")
    server = AgentHTTPServer(
        port=0, operator_token=token, static_dir=tmp_path,
        web_adapter=SimpleNamespace(), task_router=SimpleNamespace(), supervisor=SimpleNamespace(),
        manager_ops=ManagerOps(),
    )
    await server.start()
    try:
        yield server._server.sockets[0].getsockname()[1]
    finally:
        await server.stop()


async def _exchange(port: int, request: str) -> tuple[int, dict[str, str], str]:
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    try:
        writer.write(request.encode())
        await writer.drain()
        raw = (await asyncio.wait_for(reader.read(), timeout=2)).decode()
    finally:
        writer.close()
        await writer.wait_closed()
    head, _, body = raw.partition("\r\n\r\n")
    lines = head.split("\r\n")
    headers = {key.lower(): value for key, value in (line.split(": ", 1) for line in lines[1:])}
    return int(lines[0].split()[1]), headers, body


def _request(port: int, line: str, headers: dict[str, str] | None = None, body: str = "") -> str:
    fields = {"Host": f"127.0.0.1:{port}", **(headers or {})}
    if body:
        fields.update({"Content-Type": "application/json", "Content-Length": str(len(body.encode()))})
    return f"{line} HTTP/1.1\r\n" + "".join(f"{k}: {v}\r\n" for k, v in fields.items()) + "\r\n" + body


@pytest.mark.parametrize(
    ("line", "credential", "expected"),
    [
        ("GET /", None, 200),
        ("GET /api/nothing-here", None, 401),
        ("GET /api/nothing-here", "wrong", 401),
        ("GET /api/nothing-here", "operator", 404),
        ("GET /api/nothing-here", "cookie", 404),
        ("GET /api/nothing-here", "other_port", 401),
        ("GET /api/nothing-here", "agent", 401),
        ("GET /api/nothing-here", "agent_and_cookie", 404),
        ("GET /api/nothing-here", "mixed_case_bearer", 404),
        ("PUT /api/tasks/task-1/roles/coder", None, 401),
        ("PUT /api/tasks/task-1/roles/coder", "agent", 401),
        ("OPTIONS /api/tasks/create", None, 401),
        ("GET /api/agent/v1/tools", "agent", 200),
        ("GET /api/agent/v1/tools", "operator", 401),
        ("GET /api/agent/v1/tools", None, 401),
    ],
)
def test_route_credentials(tmp_path: Path, line: str, credential: str | None, expected: int) -> None:
    async def run() -> None:
        async with _server(tmp_path) as port:
            credentials = {
                None: {},
                "wrong": {"Authorization": "Bearer wrong"},
                "operator": {"Authorization": f"Bearer {TOKEN}"},
                "agent": {"Authorization": f"Bearer {AGENT}"},
                "cookie": {"Cookie": f"other=value; looper_operator_{port}={TOKEN}"},
                "other_port": {"Cookie": f"looper_operator_{port + 1}={TOKEN}"},
                "agent_and_cookie": {
                    "Authorization": f"Bearer {AGENT}", "Cookie": f"looper_operator_{port}={TOKEN}",
                },
                "mixed_case_bearer": {"Authorization": f"bEaReR {TOKEN}"},
            }
            body = '{"prompt": "changed"}' if line.startswith("PUT ") else ""
            status, headers, answer = await _exchange(port, _request(port, line, credentials[credential], body))
            assert status == expected
            if status == 401:
                assert "operator-token" not in answer
                assert "error" in json.loads(answer)
                if "/api/agent/v1/" not in line:
                    assert headers["www-authenticate"] == 'Bearer realm="looper"'
            if line == "GET /api/agent/v1/tools" and status == 200:
                assert json.loads(answer) == {"tools": []}

    asyncio.run(run())


def test_upload_is_refused_before_reading_the_body(tmp_path: Path) -> None:
    async def run() -> None:
        async with _server(tmp_path) as port:
            status, _, _ = await _exchange(port, _request(
                port, "POST /api/config/imports", {"Content-Length": "1000000"},
            ))
            assert status == 401

    asyncio.run(run())


def test_subprocess_with_inherited_environment_cannot_write_operator_routes(tmp_path: Path) -> None:
    script = """
import sys
import urllib.error
import urllib.request
request = urllib.request.Request(sys.argv[1], data=b'{}', method='PUT',
                                 headers={'Content-Type': 'application/json'})
try:
    with urllib.request.urlopen(request, timeout=5) as response:
        print(response.status)
except urllib.error.HTTPError as error:
    print(error.code)
"""

    async def run() -> None:
        async with _server(tmp_path) as port:
            result = await asyncio.to_thread(
                subprocess.run,
                [sys.executable, "-c", script, f"http://127.0.0.1:{port}/api/tasks/task-1/roles/coder"],
                env=dict(os.environ), capture_output=True, text=True, timeout=10,
            )
            assert result.returncode == 0, result.stderr
            assert result.stdout.strip() == "401"

    asyncio.run(run())


def _assert_cookie(headers: dict[str, str], port: int) -> None:
    cookie, *attributes = headers["set-cookie"].split("; ")
    assert cookie == f"looper_operator_{port}={TOKEN}"
    assert set(attributes) == {"Path=/", "HttpOnly", "SameSite=Strict"}
    assert headers["cache-control"] == "no-store"


@pytest.mark.parametrize("path", ["/", "/index.html", "/tasks/task-1"])
def test_page_navigation_signs_in(tmp_path: Path, path: str) -> None:
    async def run() -> None:
        async with _server(tmp_path) as port:
            status, headers, body = await _exchange(port, _request(port, f"GET {path}", NAVIGATE))
            assert status == 200
            assert "dashboard" in body
            _assert_cookie(headers, port)

    asyncio.run(run())


@pytest.mark.parametrize(
    ("path", "headers"),
    [
        ("/", {}),
        ("/", {**NAVIGATE, "Sec-Fetch-Dest": "iframe"}),
        ("/assets/app.js", {**NAVIGATE, "Sec-Fetch-Dest": "script"}),
        ("/", {**NAVIGATE, "X-Forwarded-For": "10.0.0.9"}),
        ("/", {**NAVIGATE, "Forwarded": "for=10.0.0.9"}),
    ],
)
def test_other_requests_do_not_sign_in(tmp_path: Path, path: str, headers: dict[str, str]) -> None:
    async def run() -> None:
        async with _server(tmp_path) as port:
            status, response, _ = await _exchange(port, _request(port, f"GET {path}", headers))
            assert status == 200
            assert "set-cookie" not in response
            assert response["cache-control"] == ("public, max-age=3600" if path.endswith(".js") else "no-store")

    asyncio.run(run())


@pytest.mark.parametrize(
    ("host", "peer", "expected"),
    [
        ("127.0.0.1:18834", "10.0.0.5", False),
        ("evil.example:18834", "127.0.0.1", False),
        ("[::1]:18834", "::1", True),
        ("localhost:18834", "127.0.0.1", True),
        ("127.0.0.1:18834", None, False),
    ],
)
def test_sign_in_requires_loopback_host_and_peer(host: str, peer: str | None, expected: bool) -> None:
    headers = {"host": host, **{k.lower(): v for k, v in NAVIGATE.items()}}
    assert operator_auth.signs_in(headers, peer_host=peer) is expected


@pytest.mark.parametrize(
    ("path", "expected", "location"),
    [
        (f"/?token={TOKEN}", 303, "/"),
        (f"/?token={TOKEN}&x=1", 303, "/?x=1"),
        ("/?token=wrong", 401, None),
        ("/?token=", 401, None),
    ],
)
def test_sign_in_link(tmp_path: Path, path: str, expected: int, location: str | None) -> None:
    async def run() -> None:
        async with _server(tmp_path) as port:
            status, headers, body = await _exchange(port, _request(port, f"GET {path}"))
            assert status == expected
            if location is not None:
                assert headers["location"] == location
                assert headers["content-length"] == "0"
                assert body == ""
                _assert_cookie(headers, port)
            else:
                assert json.loads(body) == {"error": operator_auth.WRONG}
                assert "set-cookie" not in headers

    asyncio.run(run())


def test_server_without_operator_token_refuses_api_and_does_not_sign_in(tmp_path: Path) -> None:
    async def run() -> None:
        async with _server(tmp_path, token=None) as port:
            status, _, _ = await _exchange(port, _request(
                port, "GET /api/nothing-here", {"Authorization": f"Bearer {TOKEN}"},
            ))
            assert status == 401
            status, headers, _ = await _exchange(port, _request(port, "GET /", NAVIGATE))
            assert status == 200
            assert "set-cookie" not in headers

    asyncio.run(run())


def test_token_is_created_private_and_kept(tmp_path: Path) -> None:
    runtime_root = tmp_path / "runtime"
    token = operator_auth.ensure_token(runtime_root)
    path = operator_auth.token_path(runtime_root)
    assert path == runtime_root / "operator-token"
    assert re.fullmatch(r"[A-Za-z0-9_-]{32,}", token)
    assert path.read_text() == token + "\n"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert operator_auth.ensure_token(runtime_root) == token
    assert operator_auth.read_token(runtime_root) == token
    assert token not in os.environ.values()


def test_token_permissions_are_tightened(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    path = operator_auth.token_path(tmp_path)
    path.write_text(TOKEN + "\n")
    path.chmod(0o644)
    assert operator_auth.ensure_token(tmp_path) == TOKEN
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert any(record.levelname == "WARNING" for record in caplog.records)


def test_invalid_token_names_the_file_and_explains_recovery(tmp_path: Path) -> None:
    path = operator_auth.token_path(tmp_path)
    path.write_text("not a token!")
    with pytest.raises(operator_auth.OperatorTokenError) as error:
        operator_auth.ensure_token(tmp_path)
    assert str(path) in str(error.value)
    assert "delete it and start looper serve again to make a new one" in str(error.value)


@pytest.mark.parametrize("kind", ["symlink", "directory"])
def test_token_must_be_a_regular_file(tmp_path: Path, kind: str) -> None:
    path = operator_auth.token_path(tmp_path)
    if kind == "symlink":
        target = tmp_path / "target"
        target.write_text(TOKEN)
        path.symlink_to(target)
    else:
        path.mkdir()
    with pytest.raises(operator_auth.OperatorTokenError):
        operator_auth.ensure_token(tmp_path)


def test_missing_token_is_not_created_by_reader(tmp_path: Path) -> None:
    assert operator_auth.read_token(tmp_path) is None
    assert not operator_auth.token_path(tmp_path).exists()
