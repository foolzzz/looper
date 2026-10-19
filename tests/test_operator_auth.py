"""The operator's API credential does not grant access to agent tools, or reach agents."""

from __future__ import annotations

import asyncio
import json
import os
import re
import stat
import subprocess
import sys
import threading
import urllib.parse
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
        ("OPTIONS /api/agent/v1/tools", None, 401),
        ("OPTIONS /api/agent/v1/tools", "agent", 404),
        ("OPTIONS /api/tasks/create", "operator", 404),
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
import urllib.parse
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
    assert headers["referrer-policy"] == "no-referrer"


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


class MemoryWriter:
    def __init__(self) -> None:
        self.data = bytearray()

    def write(self, data: bytes) -> None:
        self.data.extend(data)

    def get_extra_info(self, name: str) -> tuple[str, int]:
        return ("127.0.0.1", 12345)

    def close(self) -> None:
        pass

    async def drain(self) -> None:
        pass

    async def wait_closed(self) -> None:
        pass


@pytest.fixture(params=["memory", "socket"])
def transport(request: pytest.FixtureRequest) -> str:
    return request.param


@asynccontextmanager
async def _login_server(tmp_path: Path, transport: str) -> AsyncIterator[AgentHTTPServer]:
    (tmp_path / "index.html").write_text("<html>dashboard</html>")
    server = AgentHTTPServer(
        port=0, operator_token=TOKEN, static_dir=tmp_path, runtime_root=tmp_path,
        web_adapter=SimpleNamespace(), task_router=SimpleNamespace(), supervisor=SimpleNamespace(),
        manager_ops=ManagerOps(), dashboard_url="https://dashboard.example/prefix",
    )
    if transport == "socket":
        await server.start()
    try:
        yield server
    finally:
        if transport == "socket":
            await server.stop()


async def _send(server: AgentHTTPServer, line: str, headers: dict[str, str] | None = None
                ) -> tuple[int, dict[str, str], str]:
    request = _request(server._port, line, headers)
    if server._server is not None:
        return await _exchange(server._port, request)
    reader = asyncio.StreamReader()
    reader.feed_data(request.encode())
    reader.feed_eof()
    writer = MemoryWriter()
    await server._handle_connection(reader, writer)
    head, _, body = writer.data.decode().partition("\r\n\r\n")
    lines = head.split("\r\n")
    fields = {key.lower(): value for key, value in (line.split(": ", 1) for line in lines[1:])}
    return int(lines[0].split()[1]), fields, body


async def _issue(server: AgentHTTPServer) -> str:
    status, _, body = await _send(server, "POST /api/auth/login-codes", {"Authorization": f"Bearer {TOKEN}"})
    assert status == 201
    result = json.loads(body)
    assert result["expires_in_seconds"] == 300
    assert result["url"].startswith("https://dashboard.example/prefix/?code=")
    assert TOKEN not in result["url"]
    return urllib.parse.parse_qs(urllib.parse.urlsplit(result["url"]).query)["code"][0]


@pytest.mark.parametrize(("credential", "expected"), [(None, 401), (AGENT, 401), (TOKEN, 201)])
def test_login_code_requires_operator(tmp_path: Path, transport: str, credential: str | None, expected: int) -> None:
    async def run() -> None:
        async with _login_server(tmp_path, transport) as server:
            status, _, body = await _send(server, "POST /api/auth/login-codes",
                                         {"Authorization": f"Bearer {credential}"} if credential else {})
            assert status == expected
            if status == 201:
                code = urllib.parse.parse_qs(urllib.parse.urlsplit(json.loads(body)["url"]).query)["code"][0]
                assert (await _send(server, f"GET /?code={code}"))[0] == 303
                assert (await _send(server, f"GET /?code={code}"))[0] == 401

    asyncio.run(run())


@pytest.mark.parametrize(("path", "location"), [
    ("/", "/"), ("/index.html", "/index.html"), ("/tasks?x=1", "/tasks?x=1"),
    ("////evil.example/", "/"), ("/\\evil.example", "/"),
])
def test_sign_in_link(tmp_path: Path, transport: str, path: str, location: str) -> None:
    async def run() -> None:
        async with _login_server(tmp_path, transport) as server:
            code = await _issue(server)
            separator = "&" if "?" in path else "?"
            status, headers, body = await _send(server, f"GET {path}{separator}code={code}")
            assert status == 303
            assert headers["location"] == location
            assert headers["content-length"] == "0"
            assert body == ""
            _assert_cookie(headers, server._port)
            assert (await _send(server, f"GET /?code={code}"))[0] == 401

    asyncio.run(run())


@pytest.mark.parametrize("kind", ["wrong", "empty", "expired", "used"])
def test_invalid_login_code(tmp_path: Path, transport: str, kind: str) -> None:
    async def run() -> None:
        async with _login_server(tmp_path, transport) as server:
            now = [100.0]
            server._login_codes = operator_auth.LoginCodes(clock=lambda: now[0])
            code = await _issue(server)
            if kind == "used":
                assert (await _send(server, f"GET /?code={code}"))[0] == 303
            elif kind == "expired":
                now[0] += 300
            else:
                code = "wrong" if kind == "wrong" else ""
            status, headers, body = await _send(server, f"GET /?code={code}")
            assert status == 401
            assert "set-cookie" not in headers
            assert headers["cache-control"] == "no-store"
            assert headers["referrer-policy"] == "no-referrer"
            assert json.loads(body) == {"error": operator_auth.LOGIN_CODE_REFUSAL}

    asyncio.run(run())


def test_token_query_does_not_sign_in(tmp_path: Path, transport: str) -> None:
    async def run() -> None:
        async with _login_server(tmp_path, transport) as server:
            status, headers, _ = await _send(server, f"GET /?token={TOKEN}")
            assert status == 200
            assert "set-cookie" not in headers
            assert headers["cache-control"] == "no-store"
            assert headers["referrer-policy"] == "no-referrer"

    asyncio.run(run())


@pytest.mark.parametrize(("line", "credential", "expected"), [
    ("OPTIONS /api/agent/v1/tools", None, 401),
    ("OPTIONS /api/agent/v1/tools", AGENT, 404),
    ("OPTIONS /api/tasks/create", TOKEN, 404),
    ("OPTIONS /", None, 204),
])
def test_options_authentication(tmp_path: Path, transport: str, line: str, credential: str | None,
                                expected: int) -> None:
    async def run() -> None:
        async with _login_server(tmp_path, transport) as server:
            status, _, _ = await _send(server, line, {"Authorization": f"Bearer {credential}"} if credential else {})
            assert status == expected

    asyncio.run(run())


def test_login_codes_are_single_use_expire_and_drop_the_oldest() -> None:
    now = [100.0]
    codes = operator_auth.LoginCodes(clock=lambda: now[0])
    issued = [codes.issue() for _ in range(21)]
    assert len(codes._codes) == 20
    assert not codes.redeem(issued[0])
    assert all(codes.redeem(code) for code in issued[1:])
    assert not codes.redeem(issued[-1])
    old = codes.issue()
    now[0] += 300
    fresh = codes.issue()
    assert not codes.redeem(old)
    assert codes.redeem(fresh)
    assert not codes.redeem("\N{SNOWMAN}")


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
    assert stat.S_IMODE(runtime_root.stat().st_mode) == 0o700
    assert operator_auth.ensure_token(runtime_root) == token
    assert operator_auth.read_token(runtime_root) == token
    assert token not in os.environ.values()


def test_unsafe_runtime_root_is_refused(tmp_path: Path) -> None:
    tmp_path.chmod(0o777)
    with pytest.raises(operator_auth.OperatorTokenError, match="not writable by group or others"):
        operator_auth.ensure_token(tmp_path)
    assert not operator_auth.token_path(tmp_path).exists()


def test_group_writable_runtime_root_is_refused(tmp_path: Path) -> None:
    """Another member of the group could put something else in the token file's place."""
    tmp_path.chmod(0o775)
    with pytest.raises(operator_auth.OperatorTokenError, match="not writable by group or others"):
        operator_auth.ensure_token(tmp_path)
    assert not operator_auth.token_path(tmp_path).exists()


def test_runtime_root_owner_is_checked(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    uid = os.getuid()
    monkeypatch.setattr(operator_auth.os, "getuid", lambda: uid + 1)
    with pytest.raises(operator_auth.OperatorTokenError, match="owned by uid"):
        operator_auth.ensure_token(tmp_path)
    assert not operator_auth.token_path(tmp_path).exists()


@pytest.mark.parametrize("symlink", [False, True])
def test_existing_runtime_root_can_be_publicly_readable(tmp_path: Path, symlink: bool) -> None:
    root = tmp_path / "runtime"
    root.mkdir(mode=0o755)
    if symlink:
        link = tmp_path / "link"
        link.symlink_to(root, target_is_directory=True)
        root = link
    token = operator_auth.ensure_token(root)
    assert operator_auth.ensure_token(root) == token
    assert stat.S_IMODE(root.stat().st_mode) == 0o755


def test_token_owner_is_checked(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = operator_auth.token_path(tmp_path)
    path.write_text(TOKEN)
    path.chmod(0o600)
    real_fstat = os.fstat

    def fstat(fd: int) -> os.stat_result:
        info = real_fstat(fd)
        if stat.S_ISREG(info.st_mode):
            fields = list(info)
            fields[4] = info.st_uid + 1
            return os.stat_result(fields)
        return info

    monkeypatch.setattr(operator_auth.os, "fstat", fstat)
    with pytest.raises(operator_auth.OperatorTokenError, match="owned by uid"):
        operator_auth.ensure_token(tmp_path)


def test_token_read_is_bounded(tmp_path: Path) -> None:
    path = operator_auth.token_path(tmp_path)
    path.write_text("a" * 4097)
    path.chmod(0o600)
    with pytest.raises(operator_auth.OperatorTokenError, match="not a token"):
        operator_auth.ensure_token(tmp_path)


def test_token_permissions_are_refused_without_chmod(tmp_path: Path) -> None:
    path = operator_auth.token_path(tmp_path)
    path.write_text(TOKEN + "\n")
    path.chmod(0o644)
    with pytest.raises(operator_auth.OperatorTokenError, match="chmod 600"):
        operator_auth.ensure_token(tmp_path)
    assert stat.S_IMODE(path.stat().st_mode) == 0o644


def test_token_swap_cannot_change_the_validated_contents(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = operator_auth.token_path(tmp_path)
    path.write_text(TOKEN)
    path.chmod(0o600)
    target = tmp_path / "target"
    target.write_text(AGENT)
    swapped = False
    real_fstat, real_lstat = os.fstat, os.lstat
    lstats = 0

    def swap(info: os.stat_result) -> os.stat_result:
        nonlocal swapped
        if not swapped and stat.S_ISREG(info.st_mode):
            swapped = True
            path.unlink()
            path.symlink_to(target)
        return info

    def lstat(*args: object, **kwargs: object) -> os.stat_result:
        nonlocal lstats
        info = real_lstat(*args, **kwargs)
        lstats += 1
        # The old reader stats once for existence, then again to validate.
        return swap(info) if lstats == 2 else info

    monkeypatch.setattr(operator_auth.os, "fstat", lambda fd: swap(real_fstat(fd)))
    monkeypatch.setattr(operator_auth.os, "lstat", lstat)
    try:
        assert operator_auth.ensure_token(tmp_path) == TOKEN
    except operator_auth.OperatorTokenError:
        pass
    assert swapped


def test_invalid_token_names_the_file_and_explains_recovery(tmp_path: Path) -> None:
    path = operator_auth.token_path(tmp_path)
    path.write_text("not a token!")
    path.chmod(0o600)
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


def test_a_fifo_in_the_token_files_place_is_refused_without_blocking(tmp_path: Path) -> None:
    path = operator_auth.token_path(tmp_path)
    os.mkfifo(path, 0o600)
    outcome: list[BaseException | str] = []

    def ensure() -> None:
        try:
            outcome.append(operator_auth.ensure_token(tmp_path))
        except BaseException as exc:  # noqa: BLE001 - handed to the test thread
            outcome.append(exc)

    thread = threading.Thread(target=ensure, daemon=True)
    thread.start()
    thread.join(timeout=5)
    if thread.is_alive():  # a blocking open waits for a writer: give it one so the thread ends
        os.close(os.open(path, os.O_WRONLY | os.O_NONBLOCK))
        thread.join(timeout=5)
        pytest.fail("opening the token file blocked on a FIFO")
    assert isinstance(outcome[0], operator_auth.OperatorTokenError)
    assert "not a regular file" in str(outcome[0])


def test_missing_token_is_not_created_by_reader(tmp_path: Path) -> None:
    assert operator_auth.read_token(tmp_path) is None
    assert not operator_auth.token_path(tmp_path).exists()


def test_login_link_cli_redeems_on_the_running_server(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    from looper import cli

    operator_auth.token_path(tmp_path).write_text(TOKEN)

    async def run() -> None:
        async with _login_server(tmp_path, "socket") as server:
            code = await asyncio.to_thread(cli.main, [
                "login-link", "--port", str(server._port), "--runtime-root", str(tmp_path),
            ])
            assert code == 0
            url, message = capsys.readouterr().out.splitlines()
            assert message == "valid once, for 5 minutes"
            query = urllib.parse.urlsplit(url).query
            assert TOKEN not in url and "code=" in query
            status, headers, _ = await _send(server, f"GET /?{query}")
            assert status == 303
            _assert_cookie(headers, server._port)
            assert (await _send(server, f"GET /?{query}"))[0] == 401

    asyncio.run(run())
