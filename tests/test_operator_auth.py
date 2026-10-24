"""The operator's API credential does not grant access to agent tools, or reach agents."""

from __future__ import annotations

import asyncio
import html
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

    async def current_listing(self) -> list:
        return []

    def binding_report(self) -> dict:
        return {"enforced": False, "mismatches": 0, "by": []}


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
    headers: dict[str, str] = {}
    for line in lines[1:]:
        key, value = line.split(": ", 1)
        key = key.lower()
        headers[key] = headers[key] + "\n" + value if key in headers else value
    return int(lines[0].split()[1]), headers, body


def _request(port: int, line: str, headers: dict[str, str] | None = None, body: str = "") -> str:
    fields = {"Host": f"127.0.0.1:{port}", **(headers or {})}
    if body:
        fields.setdefault("Content-Type", "application/json")
        fields["Content-Length"] = str(len(body.encode()))
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
        ("POST /api/v1/operator_chat/stop", "agent", 401),
        ("POST /api/v1/operator_chat/turn", "agent", 401),
        ("POST /api/v1/ops/gates/list", None, 401),
        ("POST /api/v1/ops/gates/list", "agent", 401),
        ("POST /api/v1/tasks/role_update", None, 401),
        ("POST /api/v1/tasks/role_update", "agent", 401),
        ("OPTIONS /api/tasks/create", None, 401),
        ("GET /api/agent/v1/tools", "agent", 200),
        ("GET /api/agent/v1/tools", "operator", 401),
        ("GET /api/agent/v1/tools", None, 401),
        ("OPTIONS /api/agent/v1/tools", None, 401),
        ("OPTIONS /api/agent/v1/tools", "agent", 404),
        ("OPTIONS /api/tasks/create", "operator", 404),
    ],
)
def test_route_credentials(tmp_path: Path, transport: str, line: str, credential: str | None, expected: int) -> None:
    async def run() -> None:
        async with _login_server(tmp_path, transport) as server:
            port = server._port
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
            status, headers, answer = await _send(server, line, credentials[credential], body)
            assert status == expected
            if status == 401:
                assert "operator-token" not in answer
                assert "error" in json.loads(answer)
                if "/api/agent/v1/" not in line:
                    assert headers["www-authenticate"] == 'Bearer realm="looper"'
            if line == "GET /api/agent/v1/tools" and status == 200:
                assert json.loads(answer) == {"tools": []}

    asyncio.run(run())


def test_upload_is_refused_before_reading_the_body(tmp_path: Path, transport: str) -> None:
    async def run() -> None:
        async with _login_server(tmp_path, transport) as server:
            status, _, _ = await _send(server, "POST /api/config/imports", {"Content-Length": "1000000"})
            assert status == 401

    asyncio.run(run())


def test_subprocess_with_inherited_environment_cannot_write_operator_routes(tmp_path: Path) -> None:
    script = """
import sys
import urllib.parse
import urllib.error
import urllib.request
request = urllib.request.Request(sys.argv[1], data=b'{}', method='POST',
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
                [sys.executable, "-c", script, f"http://127.0.0.1:{port}/api/v1/tasks/role_update"],
                env=dict(os.environ), capture_output=True, text=True, timeout=10,
            )
            assert result.returncode == 0, result.stderr
            assert result.stdout.strip() == "401"

    asyncio.run(run())


def _assert_cookie(headers: dict[str, str], port: int, *, referrer: str = "same-origin") -> None:
    cookie, *attributes = headers["set-cookie"].split("; ")
    assert cookie == f"looper_operator_{port}={TOKEN}"
    assert set(attributes) == {"Path=/", "HttpOnly", "SameSite=Strict"}
    assert headers["cache-control"] == "no-store"
    assert headers["referrer-policy"] == referrer


@pytest.mark.parametrize("path", ["/", "/index.html", "/tasks/task-1"])
def test_page_navigation_signs_in(tmp_path: Path, transport: str, path: str) -> None:
    async def run() -> None:
        async with _login_server(tmp_path, transport) as server:
            port = server._port
            status, headers, body = await _send(server, f"GET {path}", NAVIGATE)
            assert status == 200
            assert "dashboard" in body
            _assert_cookie(headers, port)

    asyncio.run(run())


def test_a_socket_carries_the_same_exchange(tmp_path: Path) -> None:
    async def run() -> None:
        async with _login_server(tmp_path, "socket") as server:
            status, _, answer = await _send(server, "GET /api/nothing-here")
            assert status == 401 and "error" in json.loads(answer)
            status, _, _ = await _send(server, "GET /api/nothing-here", {"Authorization": f"Bearer {TOKEN}"})
            assert status == 404
            status, headers, body = await _send(server, "GET /", NAVIGATE)
            assert status == 200 and "dashboard" in body
            _assert_cookie(headers, server._port)

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
def test_other_requests_do_not_sign_in(tmp_path: Path, transport: str, path: str, headers: dict[str, str]) -> None:
    async def run() -> None:
        async with _login_server(tmp_path, transport) as server:
            status, response, _ = await _send(server, f"GET {path}", headers)
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


@pytest.fixture
def transport() -> str:
    """How a request reaches the server. Both ways run the same ``_handle_connection``, so the tests below take it
    from memory; ``test_a_socket_carries_the_same_exchange`` is the one on a real socket."""
    return "memory"


@asynccontextmanager
async def _login_server(tmp_path: Path, transport: str) -> AsyncIterator[AgentHTTPServer]:
    (tmp_path / "index.html").write_text("<html>dashboard</html>")
    server = AgentHTTPServer(
        port=0, operator_token=TOKEN, static_dir=tmp_path, runtime_root=tmp_path,
        web_adapter=SimpleNamespace(), task_router=SimpleNamespace(), supervisor=SimpleNamespace(),
        manager_ops=ManagerOps(), dashboard_url="http://dashboard.example",
    )
    (tmp_path / "assets").mkdir(exist_ok=True)
    (tmp_path / "assets" / "app.js").write_text("console.log(1)")
    if transport == "socket":
        await server.start()
    try:
        yield server
    finally:
        if transport == "socket":
            await server.stop()


async def _send(server: AgentHTTPServer, line: str, headers: dict[str, str] | None = None, body: str = ""
                ) -> tuple[int, dict[str, str], str]:
    request = _request(server._port, line, headers, body)
    if server._server is not None:
        return await _exchange(server._port, request)
    reader = asyncio.StreamReader()
    reader.feed_data(request.encode())
    reader.feed_eof()
    writer = MemoryWriter()
    await server._handle_connection(reader, writer)
    head, _, body = writer.data.decode().partition("\r\n\r\n")
    lines = head.split("\r\n")
    fields: dict[str, str] = {}
    for line in lines[1:]:
        key, value = line.split(": ", 1)
        key = key.lower()
        fields[key] = fields[key] + "\n" + value if key in fields else value
    return int(lines[0].split()[1]), fields, body


@pytest.mark.parametrize("query_key", ["token", "code"])
def test_query_does_not_sign_in(tmp_path: Path, transport: str, query_key: str) -> None:
    async def run() -> None:
        async with _login_server(tmp_path, transport) as server:
            status, headers, body = await _send(server, f"GET /?{query_key}={TOKEN}")
            assert status == 200 and "<html>dashboard</html>" not in body
            assert "set-cookie" not in headers
            assert headers["cache-control"] == "no-store"
            assert headers["referrer-policy"] == "same-origin"

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


def test_server_without_operator_token_refuses_api_and_does_not_sign_in(tmp_path: Path, transport: str) -> None:
    async def run() -> None:
        async with _login_server(tmp_path, transport) as server:
            server._operator_token = None
            status, _, _ = await _send(server, "GET /api/nothing-here", {"Authorization": f"Bearer {TOKEN}"})
            assert status == 401
            status, headers, _ = await _send(server, "GET /", NAVIGATE)
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



PASSWORD = "correct horse battery"
USER = "private-operator"


def test_password_record_is_private_scrypt_and_changes_salt(tmp_path: Path) -> None:
    root = tmp_path / "new"
    operator_auth.set_password(root, USER, PASSWORD)
    path = root / operator_auth.PASSWORD_FILE
    assert stat.S_IMODE(root.stat().st_mode) == 0o700
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    text = path.read_text()
    assert len(text.splitlines()) == 1 and PASSWORD not in text
    data = json.loads(text)
    assert set(data) == {"user", "scheme", "n", "r", "p", "salt", "hash"}
    assert (data["user"], data["scheme"], data["n"], data["r"], data["p"]) == (USER, "scrypt", 32768, 8, 1)
    record = operator_auth.load_password(root)
    assert len(record.salt) == 16 and len(record.hash) == 32
    assert operator_auth.verify(record, USER, PASSWORD)
    assert not operator_auth.verify(record, "wrong", PASSWORD)
    assert not operator_auth.verify(record, USER, "wrong")
    operator_auth.set_password(root, USER, PASSWORD)
    assert operator_auth.load_password(root).salt != record.salt
    assert operator_auth.clear_password(root)
    assert not path.exists() and operator_auth.load_password(root) is None
    assert not operator_auth.clear_password(root)


def test_verify_computes_scrypt_and_compares_both_values_for_wrong_user(tmp_path: Path,
                                                                      monkeypatch: pytest.MonkeyPatch) -> None:
    operator_auth.set_password(tmp_path, USER, PASSWORD)
    record = operator_auth.load_password(tmp_path)
    calls: list[object] = []
    scrypt, compare = operator_auth.hashlib.scrypt, operator_auth.hmac.compare_digest

    def derive(*args: object, **kwargs: object) -> bytes:
        calls.append(kwargs)
        return scrypt(*args, **kwargs)

    def digest(left: bytes, right: bytes) -> bool:
        calls.append((left, right))
        return compare(left, right)

    monkeypatch.setattr(operator_auth.hashlib, "scrypt", derive)
    monkeypatch.setattr(operator_auth.hmac, "compare_digest", digest)
    assert not operator_auth.verify(record, "wrong", PASSWORD)
    assert calls[0] == {"salt": record.salt, "n": 32768, "r": 8, "p": 1,
                        "maxmem": 64 * 1024 * 1024, "dklen": 32}
    assert calls[1:] == [(b"wrong", USER.encode()), (record.hash, record.hash)]


@pytest.mark.parametrize("kind", ["permissions", "symlink", "fifo", "directory", "owner", "oversize"])
def test_unsafe_password_file_is_refused(tmp_path: Path, kind: str, monkeypatch: pytest.MonkeyPatch) -> None:
    operator_auth.set_password(tmp_path, USER, PASSWORD)
    path = tmp_path / operator_auth.PASSWORD_FILE
    if kind == "permissions":
        path.chmod(0o644)
    elif kind == "symlink":
        target = tmp_path / "record"
        path.rename(target)
        path.symlink_to(target)
    elif kind in ("fifo", "directory"):
        path.unlink()
        os.mkfifo(path, 0o600) if kind == "fifo" else path.mkdir()
    elif kind == "oversize":
        path.write_bytes(b"x" * 4097)
    else:
        real = os.fstat

        def fstat(fd: int) -> os.stat_result:
            info = real(fd)
            if stat.S_ISREG(info.st_mode):
                fields = list(info)
                fields[4] += 1
                return os.stat_result(fields)
            return info

        monkeypatch.setattr(operator_auth.os, "fstat", fstat)
    outcomes: list[object] = []

    def load() -> None:
        try:
            outcomes.append(operator_auth.load_password(tmp_path))
        except operator_auth.OperatorTokenError as exc:
            outcomes.append(exc)

    thread = threading.Thread(target=load, daemon=True)
    thread.start()
    thread.join(2)
    if thread.is_alive():
        os.close(os.open(path, os.O_WRONLY | os.O_NONBLOCK))
        thread.join(2)
        pytest.fail("password reader blocked on FIFO")
    assert isinstance(outcomes[0], operator_auth.OperatorTokenError)
    assert str(path) in str(outcomes[0])
    if kind in ("fifo", "directory"):
        assert "not a regular file" in str(outcomes[0])
    assert "chmod 600" in str(outcomes[0]) if kind == "permissions" else "delete it" in str(outcomes[0])


@pytest.mark.parametrize(("field", "value"), [
    ("scheme", "plain"), ("user", "bad user"), ("salt", "*"), ("hash", "*"),
    ("salt", "YQ=="), ("hash", "YQ=="), ("n", 16385), ("n", 8192), ("n", 2**21),
    ("n", True), ("r", 0), ("r", 33), ("p", 0), ("p", 33), ("p", 1.0),
    ("scheme", None), ("user", None), ("n", None), ("r", None), ("p", None), ("salt", None), ("hash", None),
    ("salt", "YWFhYWFhYWFhYWFhYWFhYQ==!"), ("r", True),
])
def test_malformed_password_record_is_refused(tmp_path: Path, field: str, value: object) -> None:
    operator_auth.set_password(tmp_path, USER, PASSWORD)
    path = tmp_path / operator_auth.PASSWORD_FILE
    data = json.loads(path.read_bytes())
    if value is None:
        del data[field]
    else:
        data[field] = value
    path.write_text(json.dumps(data))
    with pytest.raises(operator_auth.OperatorTokenError, match="not a password record"):
        operator_auth.load_password(tmp_path)


@pytest.mark.parametrize("data", [b"not-json", b"[]", b"null", b"\xff"])
def test_invalid_password_json_is_refused(tmp_path: Path, data: bytes) -> None:
    path = tmp_path / operator_auth.PASSWORD_FILE
    path.write_bytes(data)
    path.chmod(0o600)
    with pytest.raises(operator_auth.OperatorTokenError, match="not a password record"):
        operator_auth.load_password(tmp_path)


@pytest.mark.parametrize("action", ["load", "set", "clear"])
def test_password_operations_refuse_unsafe_root(tmp_path: Path, action: str) -> None:
    tmp_path.chmod(0o775)
    with pytest.raises(operator_auth.OperatorTokenError, match="not writable by group or others"):
        if action == "set":
            operator_auth.set_password(tmp_path, USER, PASSWORD)
        elif action == "load":
            operator_auth.load_password(tmp_path)
        else:
            operator_auth.clear_password(tmp_path)


def test_private_write_cleans_up_after_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    operator_auth.set_password(tmp_path, USER, PASSWORD)
    before = (tmp_path / operator_auth.PASSWORD_FILE).read_bytes()

    def fail(*args: object, **kwargs: object) -> None:
        raise OSError("cannot replace")

    monkeypatch.setattr(operator_auth.os, "replace", fail)
    with pytest.raises(operator_auth.OperatorTokenError, match="cannot replace"):
        operator_auth.set_password(tmp_path, USER, "different password")
    assert (tmp_path / operator_auth.PASSWORD_FILE).read_bytes() == before
    assert list(tmp_path.iterdir()) == [tmp_path / operator_auth.PASSWORD_FILE]


def test_sessions_expire_invalidate_end_and_keep_only_100() -> None:
    now = [10.0]
    sessions = operator_auth.Sessions(clock=lambda: now[0])
    issued = [sessions.create(USER, b"hash") for _ in range(101)]
    assert len(sessions._sessions) == 100
    assert not sessions.valid(issued[0], b"hash")
    assert all(sessions.valid(value, b"hash") for value in issued[1:])
    assert re.fullmatch(r"[A-Za-z0-9_-]{43}", issued[-1])
    assert not sessions.valid(issued[-1], b"changed")
    sessions.end(issued[-1])
    assert not sessions.valid(issued[-1], b"hash")
    now[0] += 604800
    assert not sessions.valid(issued[1], b"hash")
    sessions.create(USER, b"hash")
    assert len(sessions._sessions) == 1


def test_limiter_doubles_caps_resets_and_slides() -> None:
    now = [1.0]
    limiter = operator_auth.LoginLimiter(clock=lambda: now[0])
    for _ in range(4):
        limiter.failure("peer")
        assert limiter.retry_after("peer") == 0
    for delay in (60, 120, 240, 480, 900, 900):
        limiter.failure("peer")
        assert limiter.retry_after("peer") == delay
        now[0] += delay
        assert limiter.retry_after("peer") == 0
    limiter.success("peer")
    limiter.failure("peer")
    assert limiter.retry_after("peer") == 0
    limiter = operator_auth.LoginLimiter(clock=lambda: now[0])
    for i in range(30):
        limiter.failure(str(i))
        now[0] += 1
    assert limiter.retry_after("fresh") == 0
    limiter.failure("30")
    assert limiter.retry_after("fresh") == 570
    now[0] += 570
    assert limiter.retry_after("fresh") == 0


def _policies(headers: dict[str, str]) -> None:
    assert headers["cache-control"] == "no-store"
    assert headers["referrer-policy"] == "same-origin"  # no-referrer would send Origin: null
    assert headers["content-security-policy"] == (
        "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; frame-ancestors 'none'")


async def _password_login(server: AgentHTTPServer, user: str = USER, password: str = PASSWORD,
                          **headers: str) -> tuple[int, dict[str, str], str]:
    return await _send(server, "POST /login", {
        "Origin": f"http://127.0.0.1:{server._port}", "Content-Type": "application/x-www-form-urlencoded",
        **headers,
    }, urllib.parse.urlencode({"username": user, "password": password}))


@pytest.mark.parametrize("path", ["/", "/index.html", "/tasks/task-1", "/login"])
@pytest.mark.parametrize("password_set", [False, True])
def test_login_page(tmp_path: Path, transport: str, path: str, password_set: bool) -> None:
    if password_set:
        operator_auth.set_password(tmp_path, USER, PASSWORD)

    async def run() -> None:
        async with _login_server(tmp_path, transport) as server:
            status, headers, body = await _send(server, f"GET {path}")
            assert status == 200
            _policies(headers)
            assert "<title>Looper</title>" in body and "<script" not in body
            if password_set:
                assert '<form method="post" action="/login">' in body
                assert 'name="username" value="admin"' in body
                assert 'type="password" autocomplete="current-password"' in body
            else:
                assert "<form" not in body and "No password is set." in body
                assert "looper auth set-password" in body
            status, headers, _ = await _send(server, "GET /assets/app.js")
            assert status == 200 and headers["cache-control"] == "public, max-age=3600"

    asyncio.run(run())


@pytest.mark.parametrize("secure", [False, True])
def test_password_login_session_opens_api_and_spa(tmp_path: Path, transport: str, secure: bool,
                                                  caplog: pytest.LogCaptureFixture) -> None:
    operator_auth.set_password(tmp_path, USER, PASSWORD)
    caplog.set_level("INFO")

    async def run() -> None:
        async with _login_server(tmp_path, transport) as server:
            if secure:
                server._dashboard_url = "https://dashboard.example/prefix"
            scheme = "https" if secure else "http"
            status, headers, _ = await _password_login(server, Origin=f"{scheme}://127.0.0.1:{server._port}")
            assert status == 303 and headers["location"] == "/"
            _policies(headers)
            cookie, *attributes = headers["set-cookie"].split("; ")
            assert cookie.startswith(f"looper_session_{server._port}=")
            assert cookie.split("=", 1)[1] != TOKEN
            assert set(attributes) == {"Path=/", "HttpOnly", "SameSite=Strict", "Max-Age=604800"} | (
                {"Secure"} if secure else set())
            credential = {"Cookie": cookie}
            assert (await _send(server, "GET /api/nothing-here", credential))[0] == 404
            status, page_headers, body = await _send(server, "GET /", credential)
            assert status == 200 and "dashboard" in body and "<form" not in body
            assert page_headers["cache-control"] == "no-store"
            status, headers, _ = await _send(server, "GET /login", credential)
            assert status == 303 and headers["location"] == "/"
            _policies(headers)
            assert (await _password_login(server, password="wrong",
                                          Origin=f"{scheme}://127.0.0.1:{server._port}"))[0] == 401
    asyncio.run(run())
    assert "sign-in from 127.0.0.1 succeeded" in caplog.text
    assert "sign-in from 127.0.0.1 failed" in caplog.text
    assert PASSWORD not in caplog.text and USER not in caplog.text


@pytest.mark.parametrize("kind", ["wrong_password", "wrong_user", "no_origin", "foreign_origin", "other_scheme", "null_origin_no_host",
                                  "too_large", "no_password", "unsafe", "content_type"])
def test_login_rejections(tmp_path: Path, transport: str, kind: str) -> None:
    if kind != "no_password":
        operator_auth.set_password(tmp_path, USER, PASSWORD)
    if kind == "unsafe":
        (tmp_path / operator_auth.PASSWORD_FILE).chmod(0o644)

    async def run() -> None:
        async with _login_server(tmp_path, transport) as server:
            expected = 401
            user, password = USER, PASSWORD
            headers = {"Origin": f"http://127.0.0.1:{server._port}",
                       "Content-Type": "application/x-www-form-urlencoded"}
            if kind == "wrong_user":
                user = "wrong"
            elif kind == "wrong_password":
                password = "wrong"
            elif kind == "no_origin":
                del headers["Origin"]
                expected = 403
            elif kind == "foreign_origin":
                headers["Origin"] = "http://foreign.example"
                expected = 403
            elif kind == "other_scheme":
                headers["Origin"] = f"https://127.0.0.1:{server._port}"
                expected = 403
            elif kind == "null_origin_no_host":
                headers.update(Origin="null", Host="")
                expected = 403
            elif kind == "unsafe":
                expected = 503
            elif kind == "content_type":
                headers["Content-Type"] = "application/json"
                expected = 400
            body = urllib.parse.urlencode({"username": user, "password": password})
            if kind == "too_large":
                body = "x" * 4097
                expected = 413
            status, response, answer = await _send(server, "POST /login", headers, body)
            assert status == expected
            _policies(response)
            assert "set-cookie" not in response
            if kind in ("wrong_user", "wrong_password"):
                assert "wrong user name or password" in answer
            elif kind == "no_password":
                assert "No password is set." in answer and "<form" not in answer
            elif kind == "unsafe":
                assert "the server's password file is unusable; see the server log" in html.unescape(answer)
                assert "<!doctype html>" in answer
    asyncio.run(run())


@pytest.mark.parametrize("change", ["expiry", "set", "clear"])
def test_session_is_revoked_on_api_and_pages(tmp_path: Path, transport: str, change: str) -> None:
    operator_auth.set_password(tmp_path, USER, PASSWORD)

    async def run() -> None:
        async with _login_server(tmp_path, transport) as server:
            now = [1.0]
            server._sessions = operator_auth.Sessions(clock=lambda: now[0])
            _, headers, _ = await _password_login(server)
            credential = {"Cookie": headers["set-cookie"].split(";", 1)[0]}
            assert (await _send(server, "GET /api/nothing-here", credential))[0] == 404
            if change == "expiry":
                now[0] += 604800
            elif change == "set":
                operator_auth.set_password(tmp_path, USER, "new different password")
            else:
                operator_auth.clear_password(tmp_path)
            assert (await _send(server, "GET /api/nothing-here", credential))[0] == 401
            status, _, body = await _send(server, "GET /", credential)
            assert status == 200 and "<html>dashboard</html>" not in body
            assert "No password is set." in body if change == "clear" else "<form" in body
    asyncio.run(run())


def test_logout_ends_session_and_clears_both_cookies(tmp_path: Path, transport: str) -> None:
    operator_auth.set_password(tmp_path, USER, PASSWORD)

    async def run() -> None:
        async with _login_server(tmp_path, transport) as server:
            _, headers, _ = await _password_login(server)
            credential = {"Cookie": headers["set-cookie"].split(";", 1)[0]}
            status, response, _ = await _send(server, "POST /logout", credential)
            assert status == 403
            _policies(response)
            assert (await _send(server, "GET /api/nothing-here", credential))[0] == 404
            status, response, _ = await _send(server, "POST /logout", {
                **credential, "Origin": f"http://127.0.0.1:{server._port}"})
            assert status == 303 and response["location"] == "/login"
            _policies(response)
            assert set(response["set-cookie"].splitlines()) == {
                f"{name}_{server._port}=; Path=/; Max-Age=0; HttpOnly; SameSite=Strict"
                for name in ("looper_session", "looper_operator")}
            assert (await _send(server, "GET /api/nothing-here", credential))[0] == 401
    asyncio.run(run())


def test_status_says_only_a_password_session_can_sign_out(tmp_path: Path, transport: str) -> None:
    operator_auth.set_password(tmp_path, USER, PASSWORD)

    async def run() -> None:
        async with _login_server(tmp_path, transport) as server:
            server._supervisor = SimpleNamespace(agent_status=lambda: {})
            server._web_adapter = SimpleNamespace(healthcheck=lambda: {
                "channel": "web", "healthy": True, "active_sse_streams": 0})
            _, headers, _ = await _password_login(server)
            session = headers["set-cookie"].split(";", 1)[0]
            token = f"{operator_auth.cookie_name(server._port)}={TOKEN}"
            cases = {"password session": ({"Cookie": session}, True),
                     "token cookie": ({"Cookie": token}, False),
                     "bearer token": ({"Authorization": f"Bearer {TOKEN}"}, False),
                     "both cookies": ({"Cookie": f"{session}; {token}"}, False)}
            for name, (credential, expected) in cases.items():
                status, _, body = await _send(server, "POST /api/v1/system/status", credential, "{}")
                assert status == 200 and json.loads(body)["result"]["can_sign_out"] is expected, name
    asyncio.run(run())


@pytest.mark.parametrize("line", ["OPTIONS /logout", "GET /logout", "OPTIONS /login", "PUT /login"])
def test_sign_in_methods(tmp_path: Path, transport: str, line: str) -> None:
    async def run() -> None:
        async with _login_server(tmp_path, transport) as server:
            status, headers, _ = await _send(server, line)
            assert status == 405
            _policies(headers)
    asyncio.run(run())


def test_login_limiter_prevents_scrypt_and_resets(tmp_path: Path, transport: str,
                                                monkeypatch: pytest.MonkeyPatch) -> None:
    operator_auth.set_password(tmp_path, USER, PASSWORD)
    calls: list[int] = []
    real = operator_auth.hashlib.scrypt

    def scrypt(*args: object, **kwargs: object) -> bytes:
        calls.append(1)
        return real(*args, **kwargs)

    monkeypatch.setattr(operator_auth.hashlib, "scrypt", scrypt)

    async def run() -> None:
        async with _login_server(tmp_path, transport) as server:
            now = [1.0]
            server._login_limiter = operator_auth.LoginLimiter(clock=lambda: now[0])
            for _ in range(5):
                assert (await _password_login(server, password="wrong"))[0] == 401
            assert len(calls) == 5
            for _ in range(2):
                status, headers, body = await _password_login(server)
                assert status == 429 and headers["retry-after"] == "60"
                assert "wait" in body and len(calls) == 5
                _policies(headers)
            # The lock is per TCP peer: a forwarding header names no other client.
            assert (await _password_login(server, **{"X-Forwarded-For": "10.0.0.9"}))[0] == 429
            assert len(server._login_limiter._failures) == 5
            now[0] += 60
            assert (await _password_login(server))[0] == 303
            for _ in range(4):
                assert (await _password_login(server, password="wrong"))[0] == 401
            assert (await _password_login(server))[0] == 303
    asyncio.run(run())


def test_parallel_attempts_cannot_pass_the_limiter_together(tmp_path: Path, transport: str,
                                                            monkeypatch: pytest.MonkeyPatch) -> None:
    operator_auth.set_password(tmp_path, USER, PASSWORD)
    calls: list[int] = []
    real = operator_auth.hashlib.scrypt

    def scrypt(*args: object, **kwargs: object) -> bytes:
        calls.append(1)
        return real(*args, **kwargs)

    monkeypatch.setattr(operator_auth.hashlib, "scrypt", scrypt)

    async def run() -> None:
        async with _login_server(tmp_path, transport) as server:
            answers = await asyncio.gather(*(_password_login(server, password="wrong") for _ in range(12)))
            assert sorted(status for status, _, _ in answers) == [401] * 5 + [429] * 7
            assert len(calls) == 5
    asyncio.run(run())


def test_password_cache_reloads_on_change_and_refuses_symlink(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    operator_auth.set_password(tmp_path, USER, PASSWORD)
    calls: list[Path] = []
    real = operator_auth.load_password

    def load(root: Path) -> operator_auth.PasswordRecord | None:
        calls.append(root)
        return real(root)

    monkeypatch.setattr(operator_auth, "load_password", load)
    server = AgentHTTPServer(runtime_root=tmp_path, web_adapter=SimpleNamespace(),
                             task_router=SimpleNamespace(), supervisor=SimpleNamespace())
    record = server._password_record()
    assert server._password_record() is record and len(calls) == 1
    operator_auth.set_password(tmp_path, USER, PASSWORD)
    assert server._password_record().hash != record.hash and len(calls) == 2
    path = tmp_path / operator_auth.PASSWORD_FILE
    path.chmod(0o644)
    with pytest.raises(operator_auth.OperatorTokenError):
        server._password_record()
    assert len(calls) == 3
    with pytest.raises(operator_auth.OperatorTokenError):
        server._password_record()
    assert len(calls) == 3
    path.chmod(0o600)
    assert server._password_record() and len(calls) == 4
    target = tmp_path / "target"
    path.rename(target)
    path.symlink_to(target)
    with pytest.raises(operator_auth.OperatorTokenError):
        server._password_record()


def test_local_navigation_and_token_cookie_still_open_pages(tmp_path: Path, transport: str) -> None:
    async def run() -> None:
        async with _login_server(tmp_path, transport) as server:
            for path in ("/", "/index.html", "/tasks/task-1"):
                status, headers, body = await _send(server, f"GET {path}", NAVIGATE)
                assert status == 200 and "dashboard" in body
                _assert_cookie(headers, server._port)
                status, _, body = await _send(server, f"GET {path}", {
                    "Cookie": f"looper_operator_{server._port}={TOKEN}"})
                assert status == 200 and "dashboard" in body
            status, headers, _ = await _send(server, "GET /login", NAVIGATE)
            assert status == 303 and headers["location"] == "/"
            _assert_cookie(headers, server._port)
            _policies(headers)
    asyncio.run(run())


def test_login_page_escapes_error(tmp_path: Path) -> None:
    async def run() -> None:
        async with _login_server(tmp_path, "memory") as server:
            writer = MemoryWriter()
            await server._login_page(writer, error="<script> & bad")
            assert "&lt;script&gt; &amp; bad" in writer.data.decode()
            assert "<script>" not in writer.data.decode()
            assert "No password is set." in writer.data.decode()
    asyncio.run(run())


def test_global_limiter_refuses_fresh_client_before_scrypt(tmp_path: Path, transport: str,
                                                         monkeypatch: pytest.MonkeyPatch) -> None:
    operator_auth.set_password(tmp_path, USER, PASSWORD)

    def unexpected(*args: object, **kwargs: object) -> bytes:
        pytest.fail("a globally locked client must not derive a password hash")

    monkeypatch.setattr(operator_auth.hashlib, "scrypt", unexpected)

    async def run() -> None:
        async with _login_server(tmp_path, transport) as server:
            server._login_limiter = operator_auth.LoginLimiter(clock=lambda: 1.0)
            for i in range(31):
                server._login_limiter.failure(f"10.0.0.{i}")
            status, headers, body = await _password_login(server)
            assert status == 429 and headers["retry-after"] == "600" and "wait" in body
            assert len(server._login_limiter._failures) == 31
    asyncio.run(run())


@pytest.mark.parametrize("headers", [
    {}, {**NAVIGATE, "Sec-Fetch-Dest": "iframe"}, {**NAVIGATE, "X-Forwarded-For": "10.0.0.8"},
    {**NAVIGATE, "Forwarded": "for=10.0.0.8"}, {**NAVIGATE, "Host": "10.0.0.5:1234"},
])
def test_nonlocal_navigation_requires_password(tmp_path: Path, transport: str, headers: dict[str, str]) -> None:
    operator_auth.set_password(tmp_path, USER, PASSWORD)

    async def run() -> None:
        async with _login_server(tmp_path, transport) as server:
            server._host = "0.0.0.0"
            status, response, body = await _send(server, "GET /", headers)
            assert status == 200 and "<form" in body and "set-cookie" not in response
            _policies(response)
    asyncio.run(run())


@pytest.mark.parametrize("action", ["stop", "turn"])
def test_chat_stop_paths_are_operator(action: str) -> None:
    from looper.agents import api_v1
    assert api_v1.find("POST", "/api/v1/operator_chat/" + action).operator_only


@pytest.mark.parametrize("path", ["/login/", "/logout/", "/login/extra", "/api/login"])
def test_sign_in_paths_are_exact(path: str) -> None:
    assert operator_auth.access(path) is (
        operator_auth.Access.OPERATOR if path.startswith("/api/") else operator_auth.Access.DASHBOARD)
    assert all(operator_auth.access(value) is operator_auth.Access.SIGN_IN for value in ("/login", "/logout"))


def test_password_read_uses_validated_fd(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    operator_auth.set_password(tmp_path, USER, PASSWORD)
    path = tmp_path / operator_auth.PASSWORD_FILE
    original = operator_auth.load_password(tmp_path)
    target = tmp_path / "target"
    target.write_bytes(b"bad")
    fstat = os.fstat
    swapped = False

    def swap(fd: int) -> os.stat_result:
        nonlocal swapped
        info = fstat(fd)
        if stat.S_ISREG(info.st_mode) and not swapped:
            swapped = True
            path.unlink()
            path.symlink_to(target)
        return info

    monkeypatch.setattr(operator_auth.os, "fstat", swap)
    assert operator_auth.load_password(tmp_path) == original
    assert swapped


def test_password_write_is_atomic_synced_and_handles_partial_writes(tmp_path: Path,
                                                                   monkeypatch: pytest.MonkeyPatch) -> None:
    write, fsync, replace = os.write, os.fsync, os.replace
    calls: list[str] = []

    def partial(fd: int, data: bytes) -> int:
        calls.append("write")
        return write(fd, data[:10])

    def sync(fd: int) -> None:
        calls.append("fsync")
        fsync(fd)

    def move(src: str, dst: str, *, src_dir_fd: int, dst_dir_fd: int) -> None:
        calls.append("replace")
        assert src_dir_fd == dst_dir_fd
        assert stat.S_IMODE(os.stat(src, dir_fd=src_dir_fd).st_mode) == 0o600
        replace(src, dst, src_dir_fd=src_dir_fd, dst_dir_fd=dst_dir_fd)

    monkeypatch.setattr(operator_auth.os, "write", partial)
    monkeypatch.setattr(operator_auth.os, "fsync", sync)
    monkeypatch.setattr(operator_auth.os, "replace", move)
    operator_auth.set_password(tmp_path, USER, PASSWORD)
    assert calls[-2:] == ["fsync", "replace"] and calls.count("write") > 1
    assert operator_auth.verify(operator_auth.load_password(tmp_path), USER, PASSWORD)
    assert len(list(tmp_path.iterdir())) == 1


@pytest.mark.parametrize(("field", "value"), [("n", 2**14), ("n", 2**20), ("r", 1), ("r", 32),
                                               ("p", 1), ("p", 32)])
def test_password_parameter_boundaries_load(tmp_path: Path, field: str, value: int) -> None:
    operator_auth.set_password(tmp_path, USER, PASSWORD)
    path = tmp_path / operator_auth.PASSWORD_FILE
    data = json.loads(path.read_bytes())
    data[field] = value
    path.write_text(json.dumps(data))
    assert getattr(operator_auth.load_password(tmp_path), field) == value


@pytest.mark.parametrize(("user", "password", "accepted"), [
    ("a", "12345678", True), ("a" * 64, "密码" * 4, True), ("A-_.0", "12345678", True),
    ("", "12345678", False), ("a" * 65, "12345678", False), ("a b", "12345678", False),
    ("é", "12345678", False), ("a", "1234567", False),
])
def test_password_input_bounds(tmp_path: Path, user: str, password: str, accepted: bool) -> None:
    if accepted:
        operator_auth.set_password(tmp_path, user, password)
        assert operator_auth.verify(operator_auth.load_password(tmp_path), user, password)
    else:
        with pytest.raises(ValueError):
            operator_auth.set_password(tmp_path, user, password)
        assert not (tmp_path / operator_auth.PASSWORD_FILE).exists()


@pytest.mark.parametrize("path", ["/login", "/logout"])
def test_sign_in_early_errors_have_security_headers(tmp_path: Path, transport: str, path: str) -> None:
    async def run() -> None:
        async with _login_server(tmp_path, transport) as server:
            for fields, expected in [
                ({"Content-Length": "invalid"}, 400), ({"Content-Length": "4097"}, 413),
                ({"Origin": "http://foreign.example"}, 403),
            ]:
                status, headers, _ = await _send(server, f"POST {path}", fields)
                assert status == expected
                _policies(headers)
    asyncio.run(run())


def test_logout_foreign_origin_leaves_session_valid(tmp_path: Path, transport: str) -> None:
    operator_auth.set_password(tmp_path, USER, PASSWORD)

    async def run() -> None:
        async with _login_server(tmp_path, transport) as server:
            _, headers, _ = await _password_login(server)
            credential = {"Cookie": headers["set-cookie"].split(";", 1)[0]}
            status, response, _ = await _send(server, "POST /logout", {
                **credential, "Origin": "http://foreign.example"})
            assert status == 403 and "set-cookie" not in response
            _policies(response)
            assert (await _send(server, "GET /api/nothing-here", credential))[0] == 404
    asyncio.run(run())


def test_sessions_do_not_survive_restart(tmp_path: Path, transport: str) -> None:
    operator_auth.set_password(tmp_path, USER, PASSWORD)

    async def run() -> None:
        async with _login_server(tmp_path, transport) as server:
            _, headers, _ = await _password_login(server)
            credential = {"Cookie": headers["set-cookie"].split(";", 1)[0]}
            assert (await _send(server, "GET /api/nothing-here", credential))[0] == 404
            server._sessions = operator_auth.Sessions()
            assert (await _send(server, "GET /api/nothing-here", credential))[0] == 401
    asyncio.run(run())
