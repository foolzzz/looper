"""``looper mcp-server``: the Manager's MCP server on stdin/stdout, a forwarder to ``looper serve``'s agent API.

``handle`` and ``handle_line`` run with a stub client; ``AgentClient`` and the ``python -m looper.cli mcp-server``
process run against a stub agent API on 127.0.0.1 that checks the bearer token and records each POST. No
database is needed.
"""

from __future__ import annotations

import json
import os
import queue
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.parse
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

from looper import __version__
from looper.agents import mcp_server
from looper.agents.mcp_server import AgentClient, LooperTimedOut, LooperUnavailable, handle, handle_line
from looper.agents.runtimes import RuntimeUnsupported

REPO_ROOT = Path(__file__).resolve().parents[1]
TOKEN = "agent-token-for-tests"
TOOLS = [{
    "name": "create_task",
    "description": "Create a task in a project.",
    "inputSchema": {"type": "object", "properties": {"requirement": {"type": "string"}}, "required": ["requirement"]},
}]
ANSWER = {"status": "ok", "task_id": "t-1", "summary": "已创建"}


class _StubClient:
    """Answers like AgentClient, or raises ``failure``; records each call."""

    def __init__(self, answer: dict[str, Any] | None = None, failure: LooperUnavailable | None = None) -> None:
        self.answer = answer if answer is not None else ANSWER
        self.failure = failure
        self.calls: list[tuple[str, Any]] = []

    def list_tools(self) -> list[Any]:
        self.calls.append(("list_tools", None))
        if self.failure:
            raise self.failure
        return TOOLS

    def call_tool(self, name: str, arguments: dict[str, Any], meta: dict[str, Any] | None = None
                  ) -> dict[str, Any]:
        self.calls.append((name, arguments))
        if self.failure:
            raise self.failure
        return self.answer


def _request(method: str, params: dict[str, Any] | None = None, message_id: Any = 1) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": message_id, "method": method, **({} if params is None else {"params": params})}


def _tool_result(text: str, is_error: bool) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": text}], "isError": is_error}


# --- handle, with a stub client ---

@pytest.mark.parametrize(("params", "expected_version"), [
    ({"protocolVersion": "2025-03-26", "capabilities": {}, "clientInfo": {"name": "claude-code"}}, "2025-03-26"),
    ({"capabilities": {}}, "2025-06-18"),
    (None, "2025-06-18"),
    ({"protocolVersion": 20250326}, "2025-06-18"),
], ids=["client-version", "no-version", "no-params", "non-string-version"])
def test_initialize_echoes_the_clients_protocol_version(params: dict[str, Any] | None, expected_version: str) -> None:
    assert handle(_request("initialize", params, "init-1"), _StubClient()) == {
        "jsonrpc": "2.0", "id": "init-1", "result": {
            "protocolVersion": expected_version,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": "looper", "version": __version__},
        },
    }


@pytest.mark.parametrize("message", [
    {"jsonrpc": "2.0", "method": "notifications/initialized"},
    {"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {"requestId": 3, "reason": "user"}},
    {"jsonrpc": "2.0", "method": "tools/call", "params": {"name": "create_task", "arguments": {}}},
    {"jsonrpc": "2.0", "id": 9, "result": {}},
], ids=["initialized", "cancelled", "call-without-id", "a-response"])
def test_a_message_that_needs_no_response_gets_none(message: dict[str, Any]) -> None:
    client = _StubClient()
    assert handle(message, client) is None
    assert client.calls == []


@pytest.mark.parametrize("message_id", [0, 7, "req-7", None])
def test_ping_answers_an_empty_result_with_the_same_id(message_id: Any) -> None:
    assert handle(_request("ping", message_id=message_id), _StubClient()) == {
        "jsonrpc": "2.0", "id": message_id, "result": {}}


def test_tools_list_answers_the_tools_looper_serves() -> None:
    assert handle(_request("tools/list", {}, 2), _StubClient()) == {
        "jsonrpc": "2.0", "id": 2, "result": {"tools": TOOLS}}


def test_tools_list_is_a_json_rpc_error_when_looper_does_not_answer() -> None:
    client = _StubClient(failure=LooperUnavailable("HTTP 401: bad token"))
    response = handle(_request("tools/list", message_id=2), client)
    assert response == {"jsonrpc": "2.0", "id": 2, "error": {
        "code": -32603, "message": "Looper did not answer the tools list: HTTP 401: bad token"}}


@pytest.mark.parametrize(("params", "answer", "sent_arguments", "is_error"), [
    ({"name": "create_task", "arguments": {"requirement": "写测试"}}, ANSWER, {"requirement": "写测试"}, False),
    ({"name": "create_task", "arguments": {"requirement": ""}},
     {"status": "error", "error": "requirement is empty"}, {"requirement": ""}, True),
    ({"name": "delete_project", "arguments": {"project": "p"}},
     {"status": "needs_confirmation", "confirmation": "c-1", "summary": "Delete p?"}, {"project": "p"}, False),
    ({"name": "list_projects"}, {"status": "ok", "projects": []}, {}, False),
    ({"name": "list_projects", "arguments": None}, {"status": "ok", "projects": []}, {}, False),
], ids=["ok", "status-error", "needs-confirmation", "no-arguments", "null-arguments"])
def test_tools_call_answers_loopers_body_as_the_tool_result(
    params: dict[str, Any], answer: dict[str, Any], sent_arguments: dict[str, Any], is_error: bool,
) -> None:
    client = _StubClient(answer=answer)
    assert handle(_request("tools/call", params, 3), client) == {
        "jsonrpc": "2.0", "id": 3, "result": _tool_result(json.dumps(answer, ensure_ascii=False), is_error)}
    assert client.calls == [(params["name"], sent_arguments)]


@pytest.mark.parametrize(("failure", "expected_text"), [
    (LooperUnavailable("HTTP 401: invalid agent token"),
     "Looper did not run the tool create_task: HTTP 401: invalid agent token"),
    (LooperUnavailable("no answer from http://127.0.0.1:1 ([Errno 61] Connection refused)"),
     "Looper did not run the tool create_task: no answer from http://127.0.0.1:1 ([Errno 61] Connection refused)"),
    (LooperTimedOut("no answer from http://127.0.0.1:8767 within 120 s"),
     "Looper did not answer the call to create_task: no answer from http://127.0.0.1:8767 within 120 s. "
     "It may still have run, so check its effect before calling it again."),
], ids=["http-401", "unreachable", "timed-out"])
def test_tools_call_is_an_error_tool_result_when_the_post_fails(failure: LooperUnavailable, expected_text: str) -> None:
    response = handle(_request("tools/call", {"name": "create_task", "arguments": {}}, 4), _StubClient(failure=failure))
    assert response == {"jsonrpc": "2.0", "id": 4, "result": _tool_result(expected_text, True)}


@pytest.mark.parametrize(("params", "message"), [
    (None, "tools/call needs params.name, the tool's name"),
    ({"arguments": {}}, "tools/call needs params.name, the tool's name"),
    ({"name": ""}, "tools/call needs params.name, the tool's name"),
    ({"name": 5}, "tools/call needs params.name, the tool's name"),
    ({"name": "create_task", "arguments": ["a"]}, "tools/call params.arguments must be an object"),
    ({"name": "create_task", "arguments": "requirement=x"}, "tools/call params.arguments must be an object"),
], ids=["no-params", "no-name", "empty-name", "non-string-name", "list-arguments", "string-arguments"])
def test_tools_call_with_bad_params_is_invalid_params(params: dict[str, Any] | None, message: str) -> None:
    client = _StubClient()
    assert handle(_request("tools/call", params, 5), client) == {
        "jsonrpc": "2.0", "id": 5, "error": {"code": -32602, "message": message}}
    assert client.calls == []


@pytest.mark.parametrize(("message", "error"), [
    (_request("resources/list", message_id="r-1"), {"code": -32601, "message": "method not found: resources/list"}),
    ({"jsonrpc": "2.0", "id": "r-2"}, {"code": -32600, "message": "invalid request: method must be a string"}),
], ids=["unknown-method", "no-method"])
def test_an_unknown_or_missing_method_is_an_error(message: dict[str, Any], error: dict[str, Any]) -> None:
    assert handle(message, _StubClient()) == {"jsonrpc": "2.0", "id": message["id"], "error": error}


@pytest.mark.parametrize(("line", "code"), [
    ("not json", -32700),
    (b'{"jsonrpc": "2.0", "id": 1, "method": "\xff"}', -32700),
    ('[{"jsonrpc": "2.0", "id": 1, "method": "ping"}]', -32600),
    ("42", -32600),
], ids=["not-json", "not-utf8", "array", "number"])
def test_a_line_that_is_not_a_json_object_is_answered_with_a_null_id(line: str | bytes, code: int) -> None:
    response = handle_line(line, _StubClient())
    assert response is not None and response["id"] is None and response["error"]["code"] == code


def test_a_failure_while_handling_is_an_internal_error_not_a_crash(monkeypatch: pytest.MonkeyPatch) -> None:
    def broken(*_: Any) -> None:
        raise KeyError("boom")

    monkeypatch.setattr(mcp_server, "handle", broken)
    assert handle_line('{"jsonrpc": "2.0", "id": 8, "method": "ping"}', _StubClient()) == {
        "jsonrpc": "2.0", "id": 8, "error": {"code": -32603, "message": "internal error: 'boom'"}}
    assert handle_line('{"jsonrpc": "2.0", "method": "notifications/initialized"}', _StubClient()) is None


# --- a stub agent API ---

class _AgentAPI(BaseHTTPRequestHandler):
    """``GET /api/agent/v1/tools`` and ``POST /api/agent/v1/ops/<name>``, behind the bearer token."""

    server: _AgentAPIServer

    def log_message(self, *_: Any) -> None:
        pass

    def _send(self, status: int, body: Any) -> None:
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        try:
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass  # the client gave up (the timeout case)

    def _authorized(self) -> bool:
        if self.headers.get("Authorization") == f"Bearer {TOKEN}":
            return True
        self._send(401, {"error": "invalid agent token"})
        return False

    def do_GET(self) -> None:
        if not self._authorized():
            return
        if self.path == "/api/agent/v1/tools":
            self._send(200, {"tools": TOOLS})
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self) -> None:
        if not self._authorized():
            return
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self.server.headers_seen.append({k.lower(): v for k, v in self.headers.items() if k.lower().startswith("x-looper")})
        self.server.posts.append({"path": self.path, "content_type": self.headers.get("Content-Type"), "body": body})
        name = self.path.removeprefix("/api/agent/v1/ops/")
        if name in ("slow", "looper_room_inbox"):
            time.sleep(1.0)
        status, answer = {
            "create_task": (200, ANSWER),
            "not_an_object": (200, b"[1, 2]"),
            "not_json": (200, b"<html>"),
            "created": (201, ANSWER),
            "broken": (500, b"Internal Server Error"),
            "slow": (200, ANSWER),
            "looper_room_inbox": (200, ANSWER),
        }.get(name, (404, {"error": f"unknown tool {name}"}))
        self._send(status, answer)


class _AgentAPIServer(ThreadingHTTPServer):
    posts: list[dict[str, Any]]
    headers_seen: list[dict[str, str]]


@pytest.fixture
def agent_api() -> Iterator[_AgentAPIServer]:
    server = _AgentAPIServer(("127.0.0.1", 0), _AgentAPI)
    server.posts = []
    server.headers_seen = []
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server
    server.shutdown()
    server.server_close()


def _url(server: ThreadingHTTPServer) -> str:
    return f"http://127.0.0.1:{server.server_address[1]}/"


# --- AgentClient, against the stub ---

def test_agent_client_lists_the_tools_and_posts_the_arguments(agent_api: _AgentAPIServer) -> None:
    client = AgentClient(_url(agent_api), TOKEN)
    assert client.url == _url(agent_api).rstrip("/")
    assert client.list_tools() == TOOLS
    assert client.call_tool("create_task", {"requirement": "写测试"}) == ANSWER
    assert agent_api.posts == [{"path": "/api/agent/v1/ops/create_task", "content_type": "application/json",
                                "body": {"requirement": "写测试"}}]


def test_agent_client_sends_the_binding_it_reads_at_each_call(agent_api: _AgentAPIServer, tmp_path: Path) -> None:
    turn_file = tmp_path / "turn.json"
    client = AgentClient(_url(agent_api), TOKEN, turn_file=str(turn_file))
    client.call_tool("create_task", {})  # no file yet: no header
    turn_file.write_text(json.dumps({"epoch": 1, "turn": "bind-one"}))
    client.call_tool("create_task", {})
    turn_file.write_text(json.dumps({"epoch": 2, "turn": "bind-two"}))
    client.call_tool("create_task", {}, {"claudecode/toolUseId": "toolu_1"})
    turn_file.write_text(json.dumps({"epoch": 3, "turn": None}))
    client.call_tool("create_task", {}, {"toolCallId": "call-9"})
    turn_file.write_text("{not json")
    client.call_tool("create_task", {}, {"other": "x"})
    assert agent_api.headers_seen == [
        {}, {"x-looper-turn": "bind-one"}, {"x-looper-turn": "bind-two", "x-looper-call": "toolu_1"},
        {"x-looper-call": "call-9"}, {}]


def test_tools_call_hands_the_meta_to_the_client() -> None:
    seen: list[Any] = []

    class Recording(_StubClient):
        def call_tool(self, name: str, arguments: dict[str, Any], meta: dict[str, Any] | None = None
                      ) -> dict[str, Any]:
            seen.append(meta)
            return ANSWER

    meta = {"claudecode/toolUseId": "toolu_7"}
    handle(_request("tools/call", {"name": "create_task", "arguments": {}, "_meta": meta}, 4), Recording())
    handle(_request("tools/call", {"name": "create_task", "arguments": {}}, 5), Recording())
    assert seen == [meta, None]


def test_the_process_sends_the_binding_and_logs_neither_it_nor_the_token(
        agent_api: _AgentAPIServer, tmp_path: Path) -> None:
    turn_file = tmp_path / "turn.json"
    env = _environ(LOOPER_AGENT_URL=_url(agent_api), LOOPER_AGENT_TOKEN=TOKEN, LOOPER_AGENT_TURN_FILE=str(turn_file))
    call = {"name": "create_task", "arguments": {}, "_meta": {"claudecode/toolUseId": "toolu_3"}}
    with _mcp_process(env) as mcp:
        mcp.send(INITIALIZE)
        turn_file.write_text(json.dumps({"epoch": 1, "turn": "secret-bind-1"}))
        mcp.send(_request("tools/call", call, 2))
        turn_file.write_text(json.dumps({"epoch": 2, "turn": "secret-bind-2"}))
        mcp.send(_request("tools/call", call, 3))
        _, _, stderr = mcp.close()
    assert agent_api.headers_seen == [{"x-looper-turn": "secret-bind-1", "x-looper-call": "toolu_3"},
                                      {"x-looper-turn": "secret-bind-2", "x-looper-call": "toolu_3"}]
    assert "secret-bind" not in stderr and TOKEN not in stderr


@pytest.mark.parametrize(("name", "failure", "reason"), [
    ("not_an_object", LooperUnavailable, "its answer is not a JSON object"),
    ("not_json", LooperUnavailable, "its answer is not a JSON object"),
    ("created", LooperUnavailable, "HTTP 201"),
    ("broken", LooperUnavailable, "HTTP 500"),
    ("missing", LooperUnavailable, "HTTP 404: unknown tool missing"),
    ("../tools", LooperUnavailable, "HTTP 404: unknown tool ..%2Ftools"),  # the name stays one path segment
    ("slow", LooperTimedOut, "no answer from http://127.0.0.1:{port} within 0.3 s"),
], ids=["not-an-object", "not-json", "201", "500", "404", "path-in-name", "timeout"])
def test_agent_client_raises_when_the_answer_is_unusable(
    agent_api: _AgentAPIServer, name: str, failure: type[LooperUnavailable], reason: str,
) -> None:
    client = AgentClient(_url(agent_api), TOKEN, timeout=0.3)
    with pytest.raises(failure) as raised:
        client.call_tool(name, {"a": 1})
    assert str(raised.value) == reason.format(port=agent_api.server_address[1])
    assert type(raised.value) is failure
    assert agent_api.posts[-1]["path"] == "/api/agent/v1/ops/" + urllib.parse.quote(name, safe="")


def test_a_room_inbox_that_waits_gets_its_wait_on_top_of_the_timeout(
    agent_api: _AgentAPIServer, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from looper.agents import mcp_server

    monkeypatch.setattr(mcp_server, "WAIT_MARGIN_SECONDS", 0.5)
    client = AgentClient(_url(agent_api), TOKEN, timeout=0.3)
    assert client.call_tool("looper_room_inbox", {"wait_seconds": 1}) == ANSWER  # 1 s + 0.5 s, not 0.3 s
    with pytest.raises(LooperTimedOut):
        client.call_tool("looper_room_inbox", {})  # no wait: the timeout as for any call


def test_agent_client_raises_when_nothing_listens() -> None:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    with pytest.raises(LooperUnavailable, match=rf"^no answer from http://127\.0\.0\.1:{port} \(") as raised:
        AgentClient(f"http://127.0.0.1:{port}", TOKEN).list_tools()
    assert type(raised.value) is LooperUnavailable


# --- the `looper mcp-server` process ---

def _environ(**overrides: str) -> dict[str, str]:
    """This environment without the agent variables or a proxy bypass, with a dead proxy, plus ``overrides``."""
    dropped = {"LOOPER_AGENT_URL", "LOOPER_AGENT_TOKEN", "LOOPER_AGENT_TURN_FILE", "no_proxy", "NO_PROXY"}
    env = {key: value for key, value in os.environ.items() if key not in dropped}
    # The server must reach Looper directly even when the environment names a proxy.
    return {**env, "http_proxy": "http://127.0.0.1:9", "HTTP_PROXY": "http://127.0.0.1:9", **overrides}


class _McpProcess:
    """``python -m looper.cli mcp-server``, written to and read from one line at a time."""

    def __init__(self, env: dict[str, str]) -> None:
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "looper.cli", "mcp-server"], cwd=REPO_ROOT, env=env, text=True, encoding="utf-8",
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        self.lines: queue.Queue[str | None] = queue.Queue()
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self) -> None:
        assert self.proc.stdout is not None
        for line in self.proc.stdout:
            self.lines.put(line)
        self.lines.put(None)

    def send(self, message: dict[str, Any]) -> dict[str, Any] | None:
        """Write one message; return its response line, parsed (None for a notification)."""
        assert self.proc.stdin is not None
        self.proc.stdin.write(json.dumps(message, ensure_ascii=False) + "\n")
        self.proc.stdin.flush()
        if "id" not in message:
            return None
        line = self.lines.get(timeout=30)
        assert line is not None, "the server closed stdout"
        assert line.endswith("\n") and "\n" not in line[:-1]
        return json.loads(line)

    def close(self) -> tuple[int, list[str | None], str]:
        """Close stdin; the exit code, what else reached stdout, and stderr."""
        assert self.proc.stdin is not None and self.proc.stderr is not None
        self.proc.stdin.close()
        code = self.proc.wait(timeout=30)
        rest = [self.lines.get(timeout=30)]
        while rest[-1] is not None:
            rest.append(self.lines.get(timeout=30))
        return code, rest[:-1], self.proc.stderr.read()


@contextmanager
def _mcp_process(env: dict[str, str]) -> Iterator[_McpProcess]:
    mcp = _McpProcess(env)
    try:
        yield mcp
    finally:
        if mcp.proc.poll() is None:
            mcp.proc.kill()
            mcp.proc.wait(timeout=30)


INITIALIZE = _request("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                     "clientInfo": {"name": "claude-code", "version": "2.0"}}, 1)


def test_the_process_lists_and_calls_tools_through_looper(agent_api: _AgentAPIServer) -> None:
    arguments = {"requirement": "写一个测试", "project": "looper"}
    with _mcp_process(_environ(LOOPER_AGENT_URL=_url(agent_api), LOOPER_AGENT_TOKEN=TOKEN)) as mcp:
        assert mcp.send(INITIALIZE) == {"jsonrpc": "2.0", "id": 1, "result": {
            "protocolVersion": "2025-06-18", "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": "looper", "version": __version__}}}
        assert mcp.send({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None
        assert mcp.send(_request("tools/list", {}, 2)) == {"jsonrpc": "2.0", "id": 2, "result": {"tools": TOOLS}}
        assert mcp.send(_request("tools/call", {"name": "create_task", "arguments": arguments}, 3)) == {
            "jsonrpc": "2.0", "id": 3, "result": _tool_result(json.dumps(ANSWER, ensure_ascii=False), False)}
        code, rest, stderr = mcp.close()
    assert (code, rest) == (0, [])
    assert agent_api.posts == [{"path": "/api/agent/v1/ops/create_task", "content_type": "application/json",
                                "body": arguments}]
    assert TOKEN not in stderr


def test_a_wrong_token_is_an_error_the_manager_can_read(agent_api: _AgentAPIServer) -> None:
    with _mcp_process(_environ(LOOPER_AGENT_URL=_url(agent_api), LOOPER_AGENT_TOKEN="wrong-token")) as mcp:
        mcp.send(INITIALIZE)
        listed = mcp.send(_request("tools/list", message_id=2))
        called = mcp.send(_request("tools/call", {"name": "create_task", "arguments": {"requirement": "x"}}, 3))
        code, rest, stderr = mcp.close()
    assert listed is not None and listed["error"]["code"] == -32603 and "HTTP 401" in listed["error"]["message"]
    assert called is not None and called["result"] == _tool_result(
        "Looper did not run the tool create_task: HTTP 401: invalid agent token", True)
    assert (code, rest) == (0, [])
    assert agent_api.posts == []
    assert "wrong-token" not in stderr


@pytest.mark.parametrize(("overrides", "expected"), [
    ({}, "needs LOOPER_AGENT_URL and LOOPER_AGENT_TOKEN"),
    ({"LOOPER_AGENT_TOKEN": TOKEN}, "needs LOOPER_AGENT_URL;"),
    ({"LOOPER_AGENT_URL": "http://127.0.0.1:8767", "LOOPER_AGENT_TOKEN": " "}, "needs LOOPER_AGENT_TOKEN;"),
    ({"LOOPER_AGENT_URL": "127.0.0.1:8767", "LOOPER_AGENT_TOKEN": TOKEN}, "must be an http:// or https:// URL"),
], ids=["neither", "no-url", "blank-token", "no-scheme"])
def test_the_process_exits_2_without_its_environment(overrides: dict[str, str], expected: str) -> None:
    proc = subprocess.run(
        [sys.executable, "-m", "looper.cli", "mcp-server"], cwd=REPO_ROOT, env=_environ(**overrides),
        input=json.dumps(INITIALIZE) + "\n", capture_output=True, text=True, timeout=30,
    )
    assert (proc.returncode, proc.stdout) == (2, "")
    assert expected in proc.stderr and TOKEN not in proc.stderr


@pytest.fixture
def dsh_mcp_state(tmp_path):
    from types import SimpleNamespace
    from looper.agents.ops.planner import TOOLS as PLANNER_TOOLS

    # A real stdio subprocess uses the production JSON-RPC handler with a local fake API client.
    # No TCP bind is needed, so startup failures remain testable in a network-restricted sandbox.
    script = tmp_path / "fake_mcp.py"
    script.write_text("""
import json, os, sys
from pathlib import Path
from looper.agents.mcp_server import handle_line, LooperUnavailable
state_path = Path(os.environ['TEST_MCP_STATE'])
class Client:
    def state(self):
        state = json.loads(state_path.read_text())
        if os.environ.get('LOOPER_AGENT_TOKEN') != state['current_token']:
            raise LooperUnavailable('HTTP 401: invalid agent token')
        if os.environ.get('LOOPER_AGENT_URL') != state['url']:
            raise LooperUnavailable('wrong run URL')
        return state
    def list_tools(self):
        state = self.state()
        state['lists'] += 1
        state_path.write_text(json.dumps(state))
        return state['tools']
    def call_tool(self, name, arguments, meta=None):
        state = self.state()
        state['posts'].append({'path':'/api/agent/v1/ops/'+name, 'body':arguments})
        state_path.write_text(json.dumps(state))
        return {'status':'ok'}
for line in sys.stdin:
    answer = handle_line(line, Client())
    if answer is not None:
        print(json.dumps(answer), flush=True)
""")
    return SimpleNamespace(script=script, state=tmp_path / "state.json", posts=[], model_requests=0, lists=0,
                           current_token=TOKEN, tools=[tool.listing() for tool in PLANNER_TOOLS],
                           url="http://127.0.0.1:19997/")


def _dsh_mcp_run(tmp_path, monkeypatch, server, *, token=TOKEN, mode="ok", reuse=None, role="planner"):
    """Real Cordis startup audit, official MCP client and headless runner; fake agent/model only."""
    from dataclasses import replace

    from looper.agents.library import AgentLibrary
    from looper.agents.ops.service import server_config
    from looper.agents.agent_settings import AgentType
    from looper.agents.runtimes import dsh
    from test_dsh_runtime import _prompt_launch

    node, executable = shutil.which("node"), shutil.which("dsh")
    if not node or not executable:
        pytest.skip("Installed DSH and Node are required for the real MCP startup test")
    source = tmp_path / "source.json"
    server.state.write_text(json.dumps({key: getattr(server, key) for key in
                                       ("posts", "model_requests", "lists", "current_token", "tools", "url")}))
    bridge = server_config(server.url, token)
    bridge["args"] = [str(server.script)]
    bridge["env"]["TEST_MCP_STATE"] = str(server.state)
    servers = {"looper": bridge}
    if mode == "crashed":
        servers["looper"]["args"] = ["-c", "raise SystemExit(7)"]
    if mode == "optional":
        servers["unavailable"] = {"command": sys.executable, "args": ["-c", "raise SystemExit(8)"]}
    elif mode == "sse":
        servers["unavailable"] = {"type": "sse", "url": "http://127.0.0.1:19996/sse"}
    source.write_text(json.dumps({"mcpServers": servers}))
    launch = replace(_prompt_launch(monkeypatch), role=AgentType(role),
                     library=AgentLibrary(mcp_config=str(source), role=role))
    try:
        files = dsh.bootstrap_files(executable, "test prompt", launch)
    except RuntimeUnsupported as error:
        return None, {"error": str(error)}
    home = dsh.write_home(tmp_path / "local", files, owner="planner")
    module = dsh.bootstrap_entry(home)["name"] if reuse is None else reuse
    report = tmp_path / "report.json"
    config = tmp_path / "cordis.json"
    program = r'''
import {createRequire} from 'node:module';
import {realpathSync, writeFileSync} from 'node:fs';
import {pathToFileURL} from 'node:url';
import {spawnSync} from 'node:child_process';
const [executable, moduleUrl, configPath, reportPath, apiUrl, mode] = process.argv.slice(2);
const require = createRequire(realpathSync(executable));
const load = name => import(pathToFileURL(require.resolve('@deepseek-ai/' + name)));
const {boot} = await load('dsh-app-boot');
const plugin = await import(moduleUrl);
const official = await load('dsh-mcp-client');
const runner = await load('dsh-headless');
const {scrubbedParentEnv} = await load('dsh-subprocess');
const scrubbed = scrubbedParentEnv();
if (scrubbed.LOOPER_MCP_TEST_MARKER !== 'visible') throw new Error('scrub positive control missing');
for (const key of Object.keys(process.env).filter(key => key.startsWith('LOOPER_DSH_MCP_SECRET_'))) {
  if (key in scrubbed) throw new Error('MCP secret reference leaked to tool subprocess environment');
}
for (const dep of [...official.inject, ...runner.inject, 'systemPrompt']) {
  if (!plugin.inject.includes(dep)) throw new Error('missing official dependency: '+dep);
}
if (new Set(plugin.inject).size !== plugin.inject.length) throw new Error('duplicate dependency');
const entries = [
  {id:'system-prompt', name:pathToFileURL(require.resolve('@deepseek-ai/dsh-system-prompt')).href},
  {id:'tools', name:pathToFileURL(require.resolve('@deepseek-ai/dsh-tools')).href, config:{mode:'native'}},
  {id:'headless-runner', name:moduleUrl, config:{task:'exercise the bridge'}},
];
writeFileSync(configPath, JSON.stringify(entries));
let context, finish;
const exited = new Promise(resolve => {finish=resolve;});
try {
  context = await boot('dsh-test', configPath, [], async ctx => {
    ctx.provide('appExit', code => finish(code));
    ctx.provide('agentDefaultModel', {currentSelection: () => ({provider:'fake',model:'fake'})});
    ctx.provide('sessions', {flush: async () => {}});
    ctx.provide('agents', {async create() {
      const events = [];
      let pending = Promise.resolve();
      const agent = {status:'idle', session:{get seq() {return events.length;}, eventAt:i=>events[i]},
        whenIdle:()=>pending, followup() {
          pending = (async () => {
            const model = spawnSync(process.env.DSH_TEST_PYTHON, ['-c',
              'import json,sys; from pathlib import Path; p=Path(sys.argv[1]); s=json.loads(p.read_text()); s["model_requests"]+=1; p.write_text(json.dumps(s))',
              process.env.DSH_TEST_STATE]);
            if (model.status !== 0) throw new Error('fake model process failed');
            const tool = ctx.tools.get('mcp__looper__'+process.env.DSH_TEST_CALL_TOOL);
            if (!tool) throw new Error('platform tools missing before fake model request');
            const answer = await tool.execute({}, {signal:new AbortController().signal});
            if (!answer.content) throw new Error('MCP call did not return content');
            events.push({type:'turn/start',data:{}}, {type:'turn/end',data:{reason:{kind:'completed'}}});
          })();
        }};
      return {agent};
    }});
    if (mode === 'duplicate') await ctx.plugin(official, official.Config({
      serverName:'looper', transport:'stdio', command:process.env.DSH_TEST_PYTHON,
      args:[process.env.DSH_TEST_SCRIPT],
      env:{LOOPER_AGENT_URL:apiUrl, LOOPER_AGENT_TOKEN:process.env.DSH_TEST_TOKEN, TEST_MCP_STATE:process.env.DSH_TEST_STATE},
      failOnStartupError:true, reconnect:{enabled:false},
    }));
  });
  const code = await exited;
  writeFileSync(reportPath, JSON.stringify({code, platformTools:[...plugin.platformTools],
                                           diagnostics:plugin.diagnostics, moduleUrl}));
  process.exitCode = code;
} catch(error) {
  writeFileSync(reportPath, JSON.stringify({error:error.message}));
  process.exitCode = 1;
} finally {
  await context?.fiber.dispose();
}
'''
    env = {**dsh.environment(launch, home), "DSH_TEST_PYTHON": sys.executable, "DSH_TEST_TOKEN": token,
           "PYTHONPATH": str(REPO_ROOT), "DSH_TEST_STATE": str(server.state),
           "DSH_TEST_SCRIPT": str(server.script),
           "LOOPER_MCP_TEST_MARKER": "visible",
           "DSH_TEST_CALL_TOOL": server.tools[0]["name"] if server.tools else "missing"}
    if mode == "missing-reference":
        reference = dsh.mcp_config(launch.library).servers[0]["envRefs"]["LOOPER_AGENT_TOKEN"]
        del env[reference]
    done = subprocess.run([node, "--input-type=module", "-", executable, module, str(config), str(report),
                           server.url, mode], input=program, env=env, cwd=tmp_path,
                          capture_output=True, text=True, timeout=35)
    for key, value in json.loads(server.state.read_text()).items():
        setattr(server, key, value)
    return done, json.loads(report.read_text()) if report.exists() else {"error": done.stderr}


def _dsh_role_tools(state, role):
    from looper.agents.ops import planner, room, service
    from looper.agents.room import MemberKind

    tools = {"manager": service.MANAGER_TOOLS, "planner": planner.TOOLS,
             "worker": room.tools_for(MemberKind.WORK), "reviewer": room.tools_for(MemberKind.REVIEW)}
    state.tools = [tool.listing() for tool in tools[role]]


@pytest.mark.parametrize("role", ["manager", "planner", "worker", "reviewer"])
def test_dsh_official_mcp_lists_calls_and_refreshes_token_before_model(tmp_path, monkeypatch, dsh_mcp_state, role):
    _dsh_role_tools(dsh_mcp_state, role)
    done, report = _dsh_mcp_run(tmp_path, monkeypatch, dsh_mcp_state, role=role)
    assert done.returncode == 0, (done.stderr, report)
    assert report["platformTools"] == sorted("mcp__looper__" + tool["name"] for tool in dsh_mcp_state.tools)
    assert dsh_mcp_state.model_requests == 1 and dsh_mcp_state.lists >= 1
    assert dsh_mcp_state.posts[-1]["path"] == "/api/agent/v1/ops/" + dsh_mcp_state.tools[0]["name"]
    # Reuse the immutable module with a new process environment, as on a resumed turn.
    dsh_mcp_state.current_token = "rotated-token"
    updated, readback = _dsh_mcp_run(tmp_path, monkeypatch, dsh_mcp_state, token="rotated-token", reuse=report["moduleUrl"], role=role)
    assert updated.returncode == 0, (updated.stderr, readback)
    assert dsh_mcp_state.model_requests == 2
    stale, failure = _dsh_mcp_run(tmp_path, monkeypatch, dsh_mcp_state, token=TOKEN, reuse=report["moduleUrl"], role=role)
    assert stale.returncode != 0 and "headless-runner" in failure["error"]
    assert dsh_mcp_state.model_requests == 2


@pytest.mark.parametrize(("mode", "role"), [
    ("missing-token", "planner"), ("auth", "planner"), ("missing-tool", "planner"),
    ("crashed", "planner"), ("duplicate", "planner"), ("missing-tool", "manager"),
    ("missing-tool", "worker"), ("missing-tool", "reviewer"), ("unexpected-tool", "planner"),
    ("duplicate-tool", "planner"), ("empty-tools", "planner"), ("missing-reference", "planner"),
])
def test_dsh_mandatory_mcp_failure_stops_required_runner_with_zero_model_requests(
    tmp_path, monkeypatch, dsh_mcp_state, mode, role,
):
    _dsh_role_tools(dsh_mcp_state, role)
    if mode == "missing-tool":
        dsh_mcp_state.tools = dsh_mcp_state.tools[:-1]
    elif mode == "unexpected-tool":
        dsh_mcp_state.tools.append({**dsh_mcp_state.tools[0], "name": "unexpected_platform_tool"})
    elif mode == "duplicate-tool":
        dsh_mcp_state.tools.append(dsh_mcp_state.tools[0])
    elif mode == "empty-tools":
        dsh_mcp_state.tools = []
    token = "" if mode == "missing-token" else "wrong-token" if mode == "auth" else TOKEN
    done, report = _dsh_mcp_run(tmp_path, monkeypatch, dsh_mcp_state, mode=mode, token=token, role=role)
    assert done is None or done.returncode != 0, (done.stderr, report)
    assert report.get("error")
    if done is not None:
        assert "headless-runner" in report["error"], report
    assert dsh_mcp_state.model_requests == 0 and dsh_mcp_state.posts == []


@pytest.mark.parametrize("mode", ["optional", "sse"])
def test_dsh_optional_catalog_failure_keeps_diagnostic_and_runs_model(tmp_path, monkeypatch, dsh_mcp_state, mode):
    # A read-only run (a Planner's) leaves an external server out; a Worker's connects to it.
    _dsh_role_tools(dsh_mcp_state, "worker")
    done, report = _dsh_mcp_run(tmp_path, monkeypatch, dsh_mcp_state, mode=mode, role="worker")
    assert done.returncode == 0, (done.stderr, report)
    assert dsh_mcp_state.model_requests == 1
    assert report["diagnostics"] == [{"serverName": "unavailable", "status": "failed"}]


def test_the_trace_writes_the_keys_of_meta_only(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    trace = tmp_path / "trace.jsonl"
    monkeypatch.setenv("LOOPER_MCP_TRACE", str(trace))
    meta = {"claudecode/toolUseId": "toolu-secret-value", "progressToken": "tok-secret"}
    handle(_request("tools/call", {"name": "create_task", "arguments": {}, "_meta": meta}, 1), _StubClient())
    handle(_request("tools/call", {"name": "create_task", "arguments": {}}, 2), _StubClient())
    lines = [json.loads(line) for line in trace.read_text().splitlines()]
    assert lines == [{"meta_keys": ["claudecode/toolUseId", "progressToken"]}, {"meta_keys": None}]
    assert "secret" not in trace.read_text()


def test_no_trace_without_the_variable(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("LOOPER_MCP_TRACE", raising=False)
    handle(_request("tools/call", {"name": "create_task", "arguments": {}}, 1), _StubClient())
    assert list(tmp_path.iterdir()) == []
