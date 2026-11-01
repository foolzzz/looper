"""The MCP bridge reconnects kept runs without repeating a possibly executed write."""

from __future__ import annotations

import asyncio
import http.client
import json
import logging
import socket
import sys
import threading
import time
import urllib.error
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import BytesIO
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest

from looper.agents import mcp_server
from looper.agents.http_server import AGENT_API, AgentHTTPServer
from looper.agents.mcp_server import AgentClient, handle
from looper.agents.ops import RetrySafety, room as room_ops, vault as vault_ops
from looper.agents.room import MemberKind, Seat
from looper.agents.vault import VaultConfig


class _RetryAPI(ThreadingHTTPServer):
    daemon_threads = False

    def __init__(self, address: tuple[str, int], actions: list[tuple[str, Any]], retry_safety: Any) -> None:
        super().__init__(address, _Handler, bind_and_activate=False)
        self.actions = list(actions)
        self.retry_safety = retry_safety
        self.requests: list[str] = []
        self.executions = 0
        self.release = threading.Event()


class _Handler(BaseHTTPRequestHandler):
    server: _RetryAPI

    def log_message(self, *_: Any) -> None:
        pass

    def _answer(self, status: int, payload: Any, headers: dict[str, str] | None = None) -> None:
        data = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Length", str(len(data)))
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        self.server.requests.append(self.path)
        self._answer(200, {"tools": [{"name": "read"}, {"name": "write"}], "retry_safety": self.server.retry_safety})

    def do_POST(self) -> None:
        self.rfile.read(int(self.headers["Content-Length"]))
        self.server.requests.append(self.path)
        action, value = self.server.actions.pop(0) if self.server.actions else ("ok", None)
        if action == "drop":
            self.connection.shutdown(socket.SHUT_RDWR)
            self.close_connection = True
        elif action == "timeout":
            self.server.release.wait(5)
            self.close_connection = True
        elif action == "503":
            self._answer(503, {"error": "adopting"}, {} if value is None else {"Retry-After": value})
        else:
            self.server.executions += 1
            self._answer(200, {"status": "ok"})


@contextmanager
def _api(*actions: tuple[str, Any], retry_safety: Any = None, delay: float = 0) -> Iterator[_RetryAPI]:
    # Reserve a port without listening, so the delayed-start test gets actual ECONNREFUSED.
    with socket.socket() as reserved:
        reserved.bind(("127.0.0.1", 0))
        address = reserved.getsockname()
    server = _RetryAPI(address, list(actions), retry_safety)
    stop = threading.Event()
    listening = threading.Event()

    def run() -> None:
        if stop.wait(delay):
            return
        server.server_bind()
        server.server_activate()
        listening.set()
        server.serve_forever(poll_interval=0.01)

    thread = threading.Thread(target=run)
    thread.start()
    if not delay:
        assert listening.wait(2)
    try:
        yield server
    finally:
        stop.set()
        server.release.set()
        if listening.is_set():
            server.shutdown()
        thread.join(3)
        server.server_close()
        assert not thread.is_alive()


def _client(server: _RetryAPI, *, reconnect_seconds: float = 3, timeout: float = 1) -> AgentClient:
    host, port = server.server_address
    return AgentClient(f"http://{host}:{port}", "test-run-token", reconnect_seconds=reconnect_seconds,
                       timeout=timeout)


def _call(client: AgentClient, name: str = "write") -> dict[str, Any]:
    response = handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name}}, client)
    assert response is not None
    return response["result"]


@pytest.fixture
def retry_stderr(monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    class StderrHandler(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            print(self.format(record), file=sys.stderr)

    handler = StderrHandler()
    monkeypatch.setattr(mcp_server.logger, "handlers", [handler])
    # setLevel also clears cached isEnabledFor decisions and caplog restores the level afterwards.
    caplog.set_level(logging.INFO, logger=mcp_server.__name__)


def test_a_refused_call_is_retried_until_the_server_listens(capsys, retry_stderr) -> None:
    with _api(delay=1.2) as server:
        client = _client(server)
        client._opener.open = Mock(wraps=client._opener.open)
        started = time.monotonic()
        assert _call(client)["isError"] is False
        elapsed = time.monotonic() - started
        assert 1.2 <= elapsed < 3
        assert client._opener.open.call_count >= 2
        assert server.executions == 1
    lines = [line for line in capsys.readouterr().err.splitlines() if "agent api retry" in line]
    assert len(lines) == 1
    assert "attempts=" in lines[0] and "waited=" in lines[0] and "outcome=" in lines[0]


def test_a_503_with_retry_after_is_retried_for_a_write(capsys, retry_stderr) -> None:
    with _api(("503", "0"), ("503", "0")) as server:
        started = time.monotonic()
        assert _call(_client(server))["isError"] is False
        assert time.monotonic() - started >= 1
        assert server.requests == ["/api/agent/v1/ops/write"] * 3
        assert server.executions == 1
    assert capsys.readouterr().err.count("agent api retry") == 1


def test_a_write_is_not_retried_after_it_was_sent() -> None:
    with _api(("drop", None)) as server:
        result = _call(_client(server))
        assert result["isError"] is True
        assert result["content"][0]["text"].startswith("Looper did not run the tool write: no answer from ")
        assert server.requests == ["/api/agent/v1/ops/write"]
        assert server.executions == 0


def test_a_read_only_tool_is_retried_after_a_reset() -> None:
    with _api(("drop", None), retry_safety={"read": RetrySafety.READ_ONLY.value}) as server:
        client = _client(server)
        assert len(client.list_tools()) == 2
        assert _call(client, "read")["isError"] is False
        assert server.requests == ["/api/agent/v1/tools", "/api/agent/v1/ops/read", "/api/agent/v1/ops/read"]
        assert server.executions == 1


@pytest.mark.parametrize("retry_safety", [None, "read", ["read"], {"read": True}, {"read": "unknown"}, {"read": []}])
def test_a_malformed_retry_safety_sidecar_never_authorizes_a_retry(retry_safety) -> None:
    with _api(("drop", None), retry_safety=retry_safety) as server:
        client = _client(server)
        client.list_tools()
        assert _call(client, "read")["isError"] is True
        assert server.requests.count("/api/agent/v1/ops/read") == 1


def test_a_new_tools_list_clears_the_previous_retry_safety_sidecar() -> None:
    with _api(("drop", None), retry_safety={"read": RetrySafety.READ_ONLY.value}) as server:
        client = _client(server)
        client.list_tools()
        server.retry_safety = None
        client.list_tools()
        assert _call(client, "read")["isError"] is True
        assert server.requests.count("/api/agent/v1/ops/read") == 1


def test_a_timeout_is_never_retried() -> None:
    with _api(("timeout", None), retry_safety={"read": RetrySafety.READ_ONLY.value}) as server:
        client = _client(server, timeout=0.1)
        client.list_tools()
        result = _call(client, "read")
        assert result["isError"] is True
        assert result["content"][0]["text"] == (
            f"Looper did not answer the call to read: no answer from {client.url} within 0.1 s. "
            "It may still have run, so check its effect before calling it again.")
        assert server.requests.count("/api/agent/v1/ops/read") == 1


def test_the_retry_window_is_bounded(capsys, retry_stderr) -> None:
    with _api(delay=10) as server:
        started = time.monotonic()
        result = _call(_client(server, reconnect_seconds=1))
        elapsed = time.monotonic() - started
        assert 0.9 <= elapsed < 1.8
        assert result["isError"] is True
        assert result["content"][0]["text"].startswith("Looper did not run the tool write: no answer from ")
    assert capsys.readouterr().err.count("agent api retry") == 1


def test_a_retry_that_connects_cannot_wait_beyond_the_reconnect_window() -> None:
    with _api(("timeout", None), delay=0.1) as server:
        client = _client(server, reconnect_seconds=1, timeout=120)
        client._opener.open = Mock(wraps=client._opener.open)
        started = time.monotonic()
        result = _call(client)
        assert 0.9 <= time.monotonic() - started < 1.8
        assert result["isError"] is True
        text = result["content"][0]["text"]
        assert text.startswith(f"Looper did not answer the call to write: no answer from {client.url} within ")
        assert text.endswith("It may still have run, so check its effect before calling it again.")
        assert server.requests == ["/api/agent/v1/ops/write"]
        assert client._opener.open.call_count == 2


def test_without_the_variable_a_refused_call_fails_at_once(monkeypatch, capsys, retry_stderr) -> None:
    monkeypatch.delenv("LOOPER_AGENT_RECONNECT_SECONDS", raising=False)
    with _api(delay=10) as server:
        host, port = server.server_address
        client = AgentClient(f"http://{host}:{port}", "test-run-token")
        client._opener.open = Mock(wraps=client._opener.open)
        started = time.monotonic()
        assert _call(client)["isError"] is True
        assert time.monotonic() - started < 0.5
        assert client._opener.open.call_count == 1
    assert "agent api retry" not in capsys.readouterr().err


@pytest.mark.parametrize("retry_after", [None, "soon", "0.5"])
def test_a_503_without_an_integer_retry_after_is_not_retried(retry_after) -> None:
    with _api(("503", retry_after)) as server:
        result = _call(_client(server))
        assert result["isError"] is True
        assert result["content"][0]["text"] == "Looper did not run the tool write: HTTP 503: adopting"
        assert len(server.requests) == 1


@pytest.mark.parametrize("value", ["nope", "-1", "nan", "NaN"])
def test_an_invalid_variable_exits_2(monkeypatch, capsys, value) -> None:
    monkeypatch.setenv("LOOPER_AGENT_URL", "http://127.0.0.1:1")
    monkeypatch.setenv("LOOPER_AGENT_TOKEN", "test-token")
    monkeypatch.setenv("LOOPER_AGENT_RECONNECT_SECONDS", value)
    monkeypatch.setattr(mcp_server, "log_to_stderr", Mock())
    assert mcp_server.main() == 2
    assert "LOOPER_AGENT_RECONNECT_SECONDS" in capsys.readouterr().err


@pytest.mark.parametrize(("value", "expected"), [(None, 0), ("", 0), ("1.25", 1.25), ("601", 600)])
def test_main_passes_the_reconnect_window_to_the_client(monkeypatch, value, expected) -> None:
    monkeypatch.setenv("LOOPER_AGENT_URL", "http://127.0.0.1:1")
    monkeypatch.setenv("LOOPER_AGENT_TOKEN", "test-token")
    if value is None:
        monkeypatch.delenv("LOOPER_AGENT_RECONNECT_SECONDS", raising=False)
    else:
        monkeypatch.setenv("LOOPER_AGENT_RECONNECT_SECONDS", value)
    factory = Mock(return_value=SimpleNamespace(url="http://127.0.0.1:1"))
    monkeypatch.setattr(mcp_server, "AgentClient", factory)
    monkeypatch.setattr(mcp_server, "serve", Mock(return_value=0))
    monkeypatch.setattr(mcp_server, "log_to_stderr", Mock())
    assert mcp_server.main() == 0
    assert factory.call_args.kwargs["reconnect_seconds"] == expected


@pytest.mark.parametrize("kept", [False, True, "imported"])
@pytest.mark.parametrize("room", [False, True])
def test_only_kept_grants_list_retry_safety(tmp_path, kept, room) -> None:
    ops = room_ops.RoomOps(Mock(), url="http://localhost:1234")
    token = ops.issue("task-1", Seat("worker@s1", "worker", MemberKind.WORK, run="worker-1"),
                      room=room, vault=VaultConfig(tmp_path, ("notes",), None), kept=kept is True)
    if kept == "imported":
        record = ops.export_run("worker-1")
        ops = room_ops.RoomOps(Mock(), url="http://localhost:1234")
        ops.import_pending(record)
        ops.activate_run("worker-1")
    server = SimpleNamespace(_room_ops=ops)

    async def request() -> dict[str, Any]:
        writer = SimpleNamespace(write=Mock(), drain=AsyncMock())
        await AgentHTTPServer._handle_room_api(server, "GET", f"{AGENT_API}tools",
                                              {"authorization": "Bearer " + token}, b"", writer)
        response = writer.write.call_args.args[0]
        assert response.startswith(b"HTTP/1.1 200 OK\r\n")
        return json.loads(response.split(b"\r\n\r\n", 1)[1])

    payload = asyncio.run(request())
    if not kept:
        assert "retry_safety" not in payload
    else:
        tools = (*room_ops.TOOLS, *vault_ops.TOOLS) if room else vault_ops.TOOLS
        listed = {tool["name"] for tool in payload["tools"]}
        assert payload["retry_safety"] == {tool.name: tool.retry_safety.value for tool in tools if tool.name in listed}
        if room:
            assert payload["retry_safety"][room_ops.INBOX] == RetrySafety.READ_ONLY_IF_PEEK.value
            assert payload["retry_safety"][room_ops.POST] == RetrySafety.NOT_RETRYABLE.value
            assert payload["retry_safety"][room_ops.WATCH] == RetrySafety.NOT_RETRYABLE.value


@pytest.fixture
def retry_clock(monkeypatch):
    waits = []

    def sleep(seconds):
        waits.append(seconds)

    monkeypatch.setattr(mcp_server, "time", SimpleNamespace(monotonic=lambda: sum(waits), sleep=sleep))
    return waits


@contextmanager
def _answer():
    yield SimpleNamespace(status=200, read=lambda: b'{"status":"ok","tools":[]}')


@pytest.mark.parametrize("failure", [
    ConnectionRefusedError("refused"), FileNotFoundError("socket missing"),
    urllib.error.URLError(ConnectionRefusedError("refused")),
    urllib.error.URLError(FileNotFoundError("socket missing")),
])
def test_a_failure_before_sending_is_retryable_for_any_tool(retry_clock, failure):
    client = AgentClient("http://127.0.0.1:1", "token", reconnect_seconds=2)
    client._opener.open = Mock(side_effect=[failure, _answer()])
    assert _call(client)["isError"] is False
    assert client._opener.open.call_count == 2
    assert retry_clock == [0.5]


@pytest.mark.parametrize("failure", [
    http.client.RemoteDisconnected("closed"), ConnectionResetError("reset"), BrokenPipeError("broken"),
])
@pytest.mark.parametrize("request_kind", ["tools", "read", "write"])
def test_only_read_requests_retry_other_transport_failures(retry_clock, failure, request_kind):
    client = AgentClient("http://127.0.0.1:1", "token", reconnect_seconds=2)
    client._retry_safety = {"read": RetrySafety.READ_ONLY}
    client._opener.open = Mock(side_effect=[failure, _answer()])
    if request_kind == "tools":
        assert client.list_tools() == []
    else:
        assert _call(client, request_kind)["isError"] is (request_kind == "write")
    assert client._opener.open.call_count == (1 if request_kind == "write" else 2)
    assert retry_clock == ([] if request_kind == "write" else [0.5])


def test_backoff_doubles_to_five_seconds_and_ends_at_the_window(retry_clock):
    client = AgentClient("http://127.0.0.1:1", "token", reconnect_seconds=13)
    client._opener.open = Mock(side_effect=ConnectionRefusedError("refused"))
    assert _call(client)["isError"] is True
    assert retry_clock == [0.5, 1, 2, 4, 5, 0.5]
    assert client._opener.open.call_count == 6


@pytest.mark.parametrize(("retry_after", "expected_wait"), [("-1", 0.5), ("999", 5)])
def test_retry_after_is_clamped_to_half_a_second_through_five_seconds(retry_clock, retry_after, expected_wait):
    client = AgentClient("http://127.0.0.1:1", "token", reconnect_seconds=10)
    failure = urllib.error.HTTPError(client.url, 503, "adopting", {"Retry-After": retry_after},
                                     BytesIO(b'{"error":"adopting"}'))
    client._opener.open = Mock(side_effect=[failure, _answer()])
    assert _call(client)["isError"] is False
    assert retry_clock == [expected_wait]


@pytest.mark.parametrize(("window", "expected_wait"), [(9.25, 0), (12.75, 2), (150, 120)])
def test_a_retried_inbox_wait_fits_the_remaining_window(retry_clock, window, expected_wait):
    client = AgentClient("http://127.0.0.1:1", "token", reconnect_seconds=window)
    arguments = {"peek": True, "wait_seconds": 120}
    seen = []

    def open_request(request, timeout):
        seen.append((json.loads(request.data), timeout))
        if len(seen) == 1:
            raise ConnectionRefusedError("refused")
        if len(seen) == 2:
            retry_clock.append(6.5)  # most of the short reconnect window passed before this attempt failed
            raise ConnectionRefusedError("still refused")
        return _answer()

    client._opener.open = Mock(side_effect=open_request)
    assert client.call_tool(room_ops.INBOX, arguments)["status"] == "ok"
    assert seen[0][0] == arguments
    assert seen[-1][0]["wait_seconds"] == expected_wait
    assert all(0 <= body["wait_seconds"] <= timeout for body, timeout in seen[1:])
    assert arguments == {"peek": True, "wait_seconds": 120}


@pytest.mark.parametrize(("peek", "reconnect_seconds"), [(1, 3), ("true", 3), (True, 0)])
def test_peek_retries_require_true_and_an_enabled_reconnect_window(peek, reconnect_seconds):
    with _api(("drop", None), retry_safety={room_ops.INBOX: RetrySafety.READ_ONLY_IF_PEEK.value}) as server:
        client = _client(server, reconnect_seconds=reconnect_seconds)
        client.list_tools()
        response = handle({"id": 1, "method": "tools/call", "params": {
            "name": room_ops.INBOX, "arguments": {"peek": peek},
        }}, client)
        assert response["result"]["isError"] is True
        assert server.requests.count(f"/api/agent/v1/ops/{room_ops.INBOX}") == 1
