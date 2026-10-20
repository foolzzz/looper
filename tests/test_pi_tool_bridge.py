"""The pi extension registers Looper's tools before the model starts.

``harness.mjs`` loads ``looper/agents/runtimes/pi_extension/looper.ts`` with
pi's own TypeScript loader, validates parameters and executes calls through
pi's real agent loop to inspect the final toolResult. A local HTTP API verifies
the actual requests; a harness clock advances deadlines without real waits. The
test does not read the extension as text. Missing node or pi's loader is a
skip, not a pass.
"""

from __future__ import annotations

import http.client
import json
import os
import shutil
import socketserver
import subprocess
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
EXTENSION = REPO_ROOT / "looper" / "agents" / "runtimes" / "pi_extension" / "looper.ts"
HARNESS = REPO_ROOT / "tests" / "fixtures" / "pi_bridge" / "harness.mjs"
NODE_TIMEOUT_SECONDS = 30

MANAGER_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["requirement"],
    "properties": {"requirement": {"type": "string", "minLength": 1}},
}
PLANNER_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["role"],
    "properties": {"role": {"type": "string", "enum": ["worker", "reviewer"]}},
}
RICH_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["kind"],
    "properties": {
        "kind": {"type": "string", "enum": ["work", "review"]},
        "item": {"$ref": "#/$defs/Item"},
    },
    "$defs": {
        "Item": {
            "type": "object",
            "additionalProperties": False,
            "required": ["id"],
            "properties": {"id": {"type": "string", "enum": ["a", "b"]}},
        },
    },
}


def _pi_package() -> Path | None:
    pi = shutil.which("pi")
    if pi is None:
        return None
    for parent in Path(pi).resolve().parents:
        package_json = parent / "package.json"
        if not package_json.is_file():
            continue
        try:
            meta = json.loads(package_json.read_text(encoding="utf-8"))
        except ValueError:
            continue
        if meta.get("name") == "@earendil-works/pi-coding-agent":
            return parent
    return None


@pytest.fixture(autouse=True)
def _pi_loader_or_skip() -> None:
    """Skip when the extension cannot actually be loaded. A skip is not a pass."""
    if shutil.which("node") is None:
        pytest.skip("node is not installed; pi's extension loader did not run")
    package = _pi_package()
    if package is None:
        pytest.skip("pi is not installed; pi's extension loader did not run")
    loader = package / "dist" / "core" / "extensions" / "loader.js"
    validator = package / "node_modules" / "@earendil-works" / "pi-ai" / "dist" / "utils" / "validation.js"
    if not loader.is_file() or not validator.is_file():
        pytest.skip("pi's extension loader is not in this install; not a pass")


class _State:
    def __init__(self) -> None:
        self.token = f"agent-token-{uuid4().hex}"
        self.requests: list[dict[str, Any]] = []
        self.get: tuple[int, Any, dict[str, str]] = (200, {"tools": []}, {})
        self.posts: dict[str, tuple[int, Any]] = {}
        self.post_queue: list[tuple[int, Any]] = []
        self.post_headers: dict[str, str] = {}
        self.post_behavior = "reply"
        self.disconnected = threading.Event()


class _AgentAPI(BaseHTTPRequestHandler):
    """``GET /api/agent/v1/tools`` and ``POST /api/agent/v1/ops/<name>`` on 127.0.0.1."""

    server: _AgentAPIServer

    def log_message(self, *_args: Any) -> None:
        return

    def _send(self, status: int, body: Any, headers: dict[str, str] | None = None) -> None:
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        extra = headers or {}
        self.send_response(status)
        for key, value in extra.items():
            self.send_header(key, value)
        if "Content-Type" not in extra and "Location" not in extra:
            self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _record(self, body: Any = None) -> bool:
        header = self.headers.get("Authorization")
        self.server.state.requests.append({
            "method": self.command,
            "path": self.path.split("?", 1)[0],
            "authorization": header,
            "content_type": self.headers.get("Content-Type"),
            "body": body,
        })
        if header == f"Bearer {self.server.state.token}":
            return True
        self._send(401, {"error": "invalid agent token"})
        return False

    def do_GET(self) -> None:
        if not self._record():
            return
        status, body, headers = self.server.state.get
        if self.path.split("?", 1)[0] == "/stolen":
            self._send(200, {"tools": [{"name": "stolen", "description": "followed a redirect", "inputSchema": {}}]})
            return
        self._send(status, body, headers)

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", "0") or "0")
        raw = self.rfile.read(length) if length else b""
        try:
            body = json.loads(raw) if raw else {}
        except ValueError:
            body = raw.decode("utf-8", "replace")
        if not self._record(body):
            return
        if self.server.state.post_behavior == "disconnect":
            self.close_connection = True
            return
        if self.server.state.post_behavior == "malformed":
            self.connection.sendall(b"HTTP/1.1 200 OK\r\nX-Private: never-print-this-header\x00\r\n\r\n")
            self.close_connection = True
            return
        if self.server.state.post_behavior in {"hold", "truncate"}:
            self.send_response(200)
            self.send_header("Content-Length", "100000")
            self.end_headers()
            self.wfile.write(b'{"status":')
            self.wfile.flush()
            if self.server.state.post_behavior == "hold":
                self.connection.settimeout(5)
                if self.connection.recv(1) == b"":
                    self.server.state.disconnected.set()
            self.close_connection = True
            return
        name = self.path.split("?", 1)[0].removeprefix("/api/agent/v1/ops/")
        if self.server.state.post_queue:
            status, answer = self.server.state.post_queue.pop(0)
        else:
            status, answer = self.server.state.posts.get(name, (404, {"error": f"unknown tool {name}"}))
        self._send(status, answer, self.server.state.post_headers)


class _AgentAPIServer(ThreadingHTTPServer):
    state: _State


class _Proxy(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True
    hits: int


class _ProxyHandler(socketserver.BaseRequestHandler):
    server: _Proxy

    def handle(self) -> None:
        self.server.hits += 1
        try:
            self.request.recv(65536)
            self.request.sendall(b"HTTP/1.1 500 No\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
        except OSError:
            return


@pytest.fixture
def agent_api() -> Iterator[_AgentAPIServer]:
    server = _AgentAPIServer(("127.0.0.1", 0), _AgentAPI)
    server.state = _State()
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()


@pytest.fixture
def proxy() -> Iterator[_Proxy]:
    server = _Proxy(("127.0.0.1", 0), _ProxyHandler)
    server.hits = 0
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        # Positive control: an unused sentinel only proves bypass if it really
        # receives and records requests directed at it.
        connection = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=2)
        try:
            connection.request("GET", "/sentinel-control")
            assert connection.getresponse().status == 500
            assert server.hits == 1
        finally:
            connection.close()
        server.hits = 0
        yield server
    finally:
        server.shutdown()
        server.server_close()


def _url(server: ThreadingHTTPServer) -> str:
    return f"http://127.0.0.1:{server.server_address[1]}"


def _parameters(schema: dict[str, Any]) -> dict[str, Any]:
    """pi's MCP ``toParameters``: keep the server schema, fill object type and properties."""
    bridged = dict(schema)
    if schema.get("type") is None:
        bridged["type"] = "object"
    if "properties" not in schema:
        bridged["properties"] = {}
    return bridged


def _tool(name: str, description: str, schema: dict[str, Any] | None) -> dict[str, Any]:
    tool: dict[str, Any] = {"name": name, "description": description}
    if schema is not None:
        tool["inputSchema"] = schema
    return tool


def _payload(stdout: str) -> dict[str, Any]:
    for line in reversed(stdout.splitlines()):
        if line.startswith("{"):
            return json.loads(line)
    raise AssertionError(stdout or "the harness wrote nothing")


def _environment(tmp_path: Path) -> dict[str, str]:
    return {
        "PATH": os.environ.get("PATH", ""), "HOME": str(tmp_path), "TMPDIR": str(tmp_path), "LANG": "C.UTF-8",
        "PI_CODING_AGENT_DIR": str(tmp_path / "agent"),
        "PI_CODING_AGENT_SESSION_DIR": str(tmp_path / "sessions"),
        "PI_TELEMETRY": "0", "PI_OFFLINE": "1", "PI_SKIP_VERSION_CHECK": "1",
    }


def _run(
    server: _AgentAPIServer | None,
    tmp_path: Path,
    *,
    binding: dict[str, str] | None = None,
    checks: list[dict[str, Any]] | None = None,
    calls: list[dict[str, Any]] | None = None,
    env_extra: dict[str, str] | None = None,
    extension: Path = EXTENSION,
) -> tuple[dict[str, Any], list[dict[str, Any]], str]:
    """Run the harness against ``server``. Returns its JSON, the requests it caused, and stderr."""
    scenario = tmp_path / "scenario.json"
    catalog = server.state.get[1].get("tools", []) if server and isinstance(server.state.get[1], dict) else []
    scenario.write_text(json.dumps({"checks": checks or [], "calls": calls or [], "catalog": catalog}), encoding="utf-8")
    env = _environment(tmp_path)
    if binding is not None:
        env.update(binding)
    if env_extra:
        env.update(env_extra)
    before = len(server.state.requests) if server else 0
    proc = subprocess.run(
        ["node", str(HARNESS), str(extension), str(scenario), str(tmp_path)],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=NODE_TIMEOUT_SECONDS,
        check=False,
    )
    if proc.returncode == 2:
        pytest.skip(_payload(proc.stdout).get("skip", "pi's extension loader did not run"))
    assert proc.returncode == 0, proc.stderr
    # No extension log lines may corrupt pi's stdout protocol.
    result = json.loads(proc.stdout)
    if server:
        assert server.state.token not in proc.stdout
        assert server.state.token not in proc.stderr
    return result, server.state.requests[before:] if server else [], proc.stderr


def _binding(server: _AgentAPIServer, *, slash: bool = False) -> dict[str, str]:
    url = _url(server) + ("/" if slash else "")
    return {"LOOPER_AGENT_URL": url, "LOOPER_AGENT_TOKEN": server.state.token}


def _assert_startup_failure(result: dict[str, Any], fragment: str) -> None:
    assert result["loaded"] is False
    assert result["ok"] is False
    assert result["tools"] == []
    assert result["error"].startswith("Failed to load extension:")
    assert fragment in result["error"]


def _assert_loaded_from_pi(result: dict[str, Any]) -> None:
    assert result["loaded"] is True
    assert result["extension"] == str(EXTENSION)
    assert result["loader"].endswith("/dist/core/extensions/loader.js")
    assert result["validator"].endswith("/node_modules/@earendil-works/pi-ai/dist/utils/validation.js")
    assert Path(result["loader"]).is_file()
    assert Path(result["validator"]).is_file()
    assert result["nativeNormalization"] is True


def test_each_role_catalog_is_registered_from_that_response(
    agent_api: _AgentAPIServer, tmp_path: Path,
) -> None:
    nonce = uuid4().hex
    catalogs = (
        [
            _tool("looper_tasks", f"manager list {nonce}", MANAGER_SCHEMA),
            _tool("looper_task_create", f"manager create {nonce}", {"type": "object", "properties": {}}),
            _tool(f"manager_probe_{nonce}", "a tool added by the server", {"type": "object"}),
        ],
        [
            _tool("looper_task_profiles", f"planner profiles {nonce}", PLANNER_SCHEMA),
            _tool("looper_task_roles", f"planner roles {nonce}", {"additionalProperties": False}),
            _tool(f"planner_probe_{nonce}", "a different server-only tool", {"type": "object"}),
        ],
    )
    for tools in catalogs:
        agent_api.state.get = (200, {"tools": tools}, {})
        result, seen, _stderr = _run(agent_api, tmp_path, binding=_binding(agent_api, slash=True))
        _assert_loaded_from_pi(result)
        assert result["tools"] == [{
            "name": f"mcp__looper__{tool['name']}",
            "description": tool["description"],
            "parameters": _parameters(tool["inputSchema"]),
        } for tool in tools]
        assert [(item["method"], item["path"], item["authorization"]) for item in seen] == [
            ("GET", "/api/agent/v1/tools", f"Bearer {agent_api.state.token}"),
        ]
        agent_api.state.requests.clear()


def test_an_empty_tool_list_registers_nothing(agent_api: _AgentAPIServer, tmp_path: Path) -> None:
    agent_api.state.get = (200, {"tools": []}, {})
    result, seen, _stderr = _run(agent_api, tmp_path, binding=_binding(agent_api))
    _assert_loaded_from_pi(result)
    assert result["tools"] == []
    assert [item["path"] for item in seen] == ["/api/agent/v1/tools"]


def test_a_duplicate_name_fails_startup_with_no_tools(agent_api: _AgentAPIServer, tmp_path: Path) -> None:
    agent_api.state.get = (200, {"tools": [
        _tool("looper_tasks", "first", {"type": "object"}),
        _tool("looper_tasks", "second", {"type": "object"}),
    ]}, {})
    result, seen, _stderr = _run(agent_api, tmp_path, binding=_binding(agent_api))
    _assert_startup_failure(result, 'duplicate tool name "looper_tasks"')
    assert [item["path"] for item in seen] == ["/api/agent/v1/tools"]


@pytest.mark.parametrize(("schema", "fragment"), [
    (None, 'illegal inputSchema'),
    ([], 'illegal inputSchema'),
    ("object", 'illegal inputSchema'),
    ({"type": "string"}, 'illegal inputSchema'),
    ({"type": "object", "properties": []}, 'illegal inputSchema'),
    ({"type": "object", "required": "kind"}, 'illegal inputSchema'),
    ({"type": "object", "additionalProperties": "false"}, 'illegal inputSchema'),
    ({"type": "object", "$defs": []}, 'illegal inputSchema'),
    ({"properties": {"item": {"type": "bogus"}}}, 'illegal inputSchema'),
    ({"properties": {"item": 42}}, 'illegal inputSchema'),
    ({"$defs": {"Item": {"required": "id"}}}, 'illegal inputSchema'),
    ({"properties": {"item": {"enum": "a"}}}, 'illegal inputSchema'),
    ({"properties": {"item": {"enum": []}}}, 'illegal inputSchema'),
    ({"required": ["item", "item"]}, 'illegal inputSchema'),
    ({"additionalProperties": {"type": "bogus"}}, 'illegal inputSchema'),
    ({"properties": {"item": {"items": 42}}}, 'illegal inputSchema'),
    ({"oneOf": {}}, 'illegal inputSchema'),
    ({"allOf": [{"properties": {"item": {"type": "bogus"}}}]}, 'illegal inputSchema'),
], ids=["missing", "array", "string", "type-string", "properties-array", "required-string",
        "additional-string", "defs-array", "nested-type", "property-number", "defs-required",
        "enum-string", "enum-empty", "duplicate-required", "additional-type", "items-number",
        "oneof-object", "allof-type"])
def test_an_illegal_schema_fails_startup_with_no_tools(
    agent_api: _AgentAPIServer, tmp_path: Path, schema: Any, fragment: str,
) -> None:
    tool = {"name": "looper_tasks", "description": "list"}
    if schema is not None:
        tool["inputSchema"] = schema
    agent_api.state.get = (200, {"tools": [
        _tool("looper_overview", "a valid tool that must not be registered alone", {"type": "object"}),
        tool,
    ]}, {})
    result, seen, _stderr = _run(agent_api, tmp_path, binding=_binding(agent_api))
    _assert_startup_failure(result, fragment)
    assert "looper_tasks" in result["error"]
    assert [item["path"] for item in seen] == ["/api/agent/v1/tools"]


@pytest.mark.parametrize(("name", "description", "fragment"), [
    ("", "list", "illegal name"),
    ("has space", "list", "illegal name"),
    (1, "list", "illegal name"),
    ("looper_tasks", None, "string description"),
    ("looper_tasks", 1, "string description"),
], ids=["empty-name", "space-name", "number-name", "missing-description", "number-description"])
def test_an_illegal_name_or_description_fails_startup(
    agent_api: _AgentAPIServer, tmp_path: Path, name: Any, description: Any, fragment: str,
) -> None:
    tool: dict[str, Any] = {"name": name, "inputSchema": {"type": "object"}}
    if description is not None:
        tool["description"] = description
    agent_api.state.get = (200, {"tools": [tool]}, {})
    result, _seen, _stderr = _run(agent_api, tmp_path, binding=_binding(agent_api))
    _assert_startup_failure(result, fragment)


@pytest.mark.parametrize(("status", "body", "fragment"), [
    (401, {"error": "invalid agent token"}, "HTTP 401: invalid agent token"),
    (500, b"Internal Server Error", "HTTP 500"),
    (201, {"tools": []}, "HTTP 201"),
    (200, b"[1, 2]", "its answer is not a JSON object"),
    (200, b"null", "its answer is not a JSON object"),
    (200, b'"tools"', "its answer is not a JSON object"),
    (200, b"<html>", "its answer is not a JSON object"),
    (200, {}, "its answer has no tools list"),
    (200, {"tools": None}, "its answer has no tools list"),
    (200, {"tools": {}}, "its answer has no tools list"),
    (200, {"tools": "no"}, "its answer has no tools list"),
], ids=["401", "500", "201", "array", "null", "string", "html", "no-tools", "tools-null", "tools-object",
        "tools-string"])
def test_an_unusable_list_fails_startup_with_no_tools(
    agent_api: _AgentAPIServer, tmp_path: Path, status: int, body: Any, fragment: str,
) -> None:
    if status == 401:
        agent_api.state.token = f"expected-{uuid4().hex}"
        binding = {"LOOPER_AGENT_URL": _url(agent_api), "LOOPER_AGENT_TOKEN": "wrong-token"}
    else:
        binding = _binding(agent_api)
    agent_api.state.get = (status, body, {})
    result, seen, stderr = _run(agent_api, tmp_path, binding=binding)
    _assert_startup_failure(result, fragment)
    assert "wrong-token" not in result["error"]
    assert "wrong-token" not in stderr
    assert [item["path"] for item in seen] == ["/api/agent/v1/tools"]


def test_a_server_error_cannot_echo_the_token(agent_api: _AgentAPIServer, tmp_path: Path) -> None:
    agent_api.state.get = (500, {"error": f"rejected {agent_api.state.token}"}, {})
    result, _seen, _stderr = _run(agent_api, tmp_path, binding=_binding(agent_api))
    _assert_startup_failure(result, "HTTP 500")
    assert "[redacted]" in result["error"]


def test_no_binding_is_an_empty_bridge(tmp_path: Path) -> None:
    result, seen, _stderr = _run(None, tmp_path, binding={})
    _assert_loaded_from_pi(result)
    assert result["tools"] == []
    assert seen == []


@pytest.mark.parametrize(("binding", "fragment"), [
    ({"LOOPER_AGENT_URL": "http://127.0.0.1:9"}, "LOOPER_AGENT_TOKEN"),
    ({"LOOPER_AGENT_TOKEN": "present"}, "LOOPER_AGENT_URL"),
    ({"LOOPER_AGENT_URL": "http://127.0.0.1:9", "LOOPER_AGENT_TOKEN": "  "}, "LOOPER_AGENT_TOKEN"),
    ({"LOOPER_AGENT_URL": "ftp://127.0.0.1:9/tools", "LOOPER_AGENT_TOKEN": "present"}, "http:// or https://"),
    ({"LOOPER_AGENT_URL": "not a url", "LOOPER_AGENT_TOKEN": "present"}, "http:// or https://"),
    ({"LOOPER_AGENT_URL": "  ", "LOOPER_AGENT_TOKEN": "\t"}, "LOOPER_AGENT_URL and LOOPER_AGENT_TOKEN"),
    ({"LOOPER_AGENT_TOKEN": ""}, "LOOPER_AGENT_URL and LOOPER_AGENT_TOKEN"),
], ids=["url-only", "token-only", "blank-token", "ftp", "not-a-url", "both-blank", "empty-token-only"])
def test_a_partial_or_unusable_binding_fails_before_a_request(
    tmp_path: Path, binding: dict[str, str], fragment: str,
) -> None:
    result, seen, _stderr = _run(None, tmp_path, binding=binding)
    _assert_startup_failure(result, fragment)
    assert seen == []


def test_pi_cli_stops_on_a_factory_error_before_calling_the_model(tmp_path: Path) -> None:
    """The real CLI must treat the loader's error as fatal, not just omit this extension."""
    env = _environment(tmp_path)
    env.update({"LOOPER_AGENT_URL": "invalid-binding", "LOOPER_AGENT_TOKEN": "test-token"})
    proc = subprocess.run(
        ["pi", "-p", "--mode", "json", "--offline", "--no-approve", "--no-context-files",
         "--no-extensions", "--no-mcp", "--no-skills", "--no-prompt-templates", "--no-themes",
         "--no-session", "-e", str(EXTENSION)],
        cwd=tmp_path, env=env, input="This must not reach a model.", capture_output=True, text=True,
        timeout=NODE_TIMEOUT_SECONDS, check=False,
    )
    assert proc.returncode != 0
    assert "Failed to load extension" in proc.stderr
    assert "LOOPER_AGENT_URL must be an http:// or https:// URL" in proc.stderr
    assert "test-token" not in proc.stdout + proc.stderr


def test_the_list_does_not_use_a_proxy(
    agent_api: _AgentAPIServer, proxy: _Proxy, tmp_path: Path,
) -> None:
    agent_api.state.get = (200, {"tools": [_tool("looper_tasks", "direct", {"type": "object"})]}, {})
    proxy_url = f"http://127.0.0.1:{proxy.server_address[1]}"
    result, seen, _stderr = _run(
        agent_api, tmp_path, binding=_binding(agent_api),
        env_extra={
            "http_proxy": proxy_url, "HTTP_PROXY": proxy_url,
            "https_proxy": proxy_url, "HTTPS_PROXY": proxy_url,
            "ALL_PROXY": proxy_url, "all_proxy": proxy_url,
            "NODE_USE_ENV_PROXY": "1",
        },
    )
    _assert_loaded_from_pi(result)
    assert result["tools"][0]["name"] == "mcp__looper__looper_tasks"
    assert [item["path"] for item in seen] == ["/api/agent/v1/tools"]
    assert proxy.hits == 0


def test_the_list_does_not_follow_a_redirect(agent_api: _AgentAPIServer, tmp_path: Path) -> None:
    location = f"{_url(agent_api)}/stolen"
    agent_api.state.get = (302, b"", {"Location": location})
    result, seen, _stderr = _run(agent_api, tmp_path, binding=_binding(agent_api))
    _assert_startup_failure(result, "HTTP 302")
    assert [item["path"] for item in seen] == ["/api/agent/v1/tools"]
    assert location not in result["error"]


def test_the_bridged_schema_keeps_server_keywords_and_pi_accepts_it(
    agent_api: _AgentAPIServer, tmp_path: Path,
) -> None:
    bare = {"additionalProperties": False, "required": ["note"], "properties": {"note": {"type": "string"}}}
    # ``type`` omitted on purpose: pi's normalizer adds it, and ``properties`` stay.
    omitted = {"properties": {"note": {"type": "string", "enum": ["keep", "drop"]}}}
    agent_api.state.get = (200, {"tools": [
        _tool("looper_task_role_set", "define a role, with the server's schema", RICH_SCHEMA),
        _tool("looper_overview", "bare", bare),
        _tool("looper_costs", "type filled in", omitted),
    ]}, {})
    checks = [
        {"name": "mcp__looper__looper_task_role_set", "accept": True,
         "arguments": {"kind": "work", "item": {"id": "a"}}},
        {"name": "mcp__looper__looper_task_role_set", "accept": True, "arguments": {"kind": "review"}},
        {"name": "mcp__looper__looper_task_role_set", "accept": False, "arguments": {"kind": "nope"}},
        {"name": "mcp__looper__looper_task_role_set", "accept": False, "arguments": {}},
        {"name": "mcp__looper__looper_task_role_set", "accept": False,
         "arguments": {"kind": "work", "extra": 1}},
        {"name": "mcp__looper__looper_task_role_set", "accept": False,
         "arguments": {"kind": "work", "item": {}}},
        {"name": "mcp__looper__looper_task_role_set", "accept": False,
         "arguments": {"kind": "work", "item": {"id": "c"}}},
        {"name": "mcp__looper__looper_task_role_set", "accept": False,
         "arguments": {"kind": "work", "item": {"id": "a", "extra": True}}},
        {"name": "mcp__looper__looper_overview", "accept": True, "arguments": {"note": "kept"}},
        {"name": "mcp__looper__looper_overview", "accept": False, "arguments": {}},
        {"name": "mcp__looper__looper_costs", "accept": True, "arguments": {"note": "keep"}},
        {"name": "mcp__looper__looper_costs", "accept": False, "arguments": {"note": "other"}},
    ]
    result, _seen, _stderr = _run(agent_api, tmp_path, binding=_binding(agent_api), checks=checks)
    _assert_loaded_from_pi(result)
    by_name = {tool["name"]: tool for tool in result["tools"]}
    assert by_name["mcp__looper__looper_task_role_set"]["parameters"] == _parameters(RICH_SCHEMA)
    rich = by_name["mcp__looper__looper_task_role_set"]["parameters"]
    assert rich["$defs"]["Item"]["properties"]["id"]["enum"] == ["a", "b"]
    assert rich["required"] == ["kind"]
    assert rich["additionalProperties"] is False
    assert by_name["mcp__looper__looper_overview"]["parameters"] == _parameters(bare)
    assert by_name["mcp__looper__looper_costs"]["parameters"]["type"] == "object"
    assert by_name["mcp__looper__looper_costs"]["parameters"]["properties"]["note"]["enum"] == ["keep", "drop"]
    failed = [check for check in result["checks"] if not check["ok"]]
    assert failed == []


def test_a_registered_tool_posts_its_arguments(agent_api: _AgentAPIServer, tmp_path: Path) -> None:
    agent_api.state.get = (200, {"tools": [
        _tool("looper_task_create", "create", MANAGER_SCHEMA),
    ]}, {})
    ok = {"status": "ok", "summary": "已创建"}
    confirm = {"status": "needs_confirmation", "confirmation_id": "c-1"}
    failed = {"status": "error", "error": "no"}
    agent_api.state.post_queue = [
        (200, ok),
        (200, confirm),
        (200, failed),
        (500, b"no"),
    ]
    sent = [
        {"requirement": "写测试"},
        {"requirement": "confirm"},
        {"requirement": "fail"},
        {"requirement": "broken"},
    ]
    result, seen, _stderr = _run(
        agent_api, tmp_path, binding=_binding(agent_api),
        calls=[{
            "name": "mcp__looper__looper_task_create",
            "id": f"call-{index}",
            "arguments": arguments,
        } for index, arguments in enumerate(sent)],
    )
    _assert_loaded_from_pi(result)
    assert [call["ok"] for call in result["calls"]] == [True, True, True, True]
    assert [call["isError"] for call in result["calls"]] == [False, False, True, True]
    assert all(call["detailsUndefined"] is True for call in result["calls"])
    assert json.loads(result["calls"][0]["content"][0]["text"]) == ok
    assert json.loads(result["calls"][1]["content"][0]["text"]) == confirm
    assert json.loads(result["calls"][2]["content"][0]["text"]) == failed
    assert "HTTP 500" in result["calls"][3]["content"][0]["text"]
    assert agent_api.state.token not in result["calls"][3]["content"][0]["text"]
    posts = [item for item in seen if item["method"] == "POST"]
    assert posts == [{
        "method": "POST",
        "path": "/api/agent/v1/ops/looper_task_create",
        "authorization": f"Bearer {agent_api.state.token}",
        "content_type": "application/json",
        "body": arguments,
    } for arguments in sent]


def _call(name: str = "looper_probe", **extra: Any) -> dict[str, Any]:
    return {"name": f"mcp__looper__{name}", "id": "probe-call", "arguments": {}, **extra}


def _catalog(server: _AgentAPIServer, name: str = "looper_probe") -> None:
    server.state.get = (200, {"tools": [_tool(name, "HTTP bridge probe", {"type": "object"})]}, {})


def _assert_call(result: dict[str, Any], seen: list[dict[str, Any]], *, error: bool) -> dict[str, Any]:
    _assert_loaded_from_pi(result)
    assert result["processor"].endswith("/pi-agent-core/dist/agent-loop.js")
    assert Path(result["processor"]).is_file()
    assert len(result["calls"]) == 1
    call = result["calls"][0]
    assert call["ok"] is True, call
    assert call["isError"] is error
    assert call["executionIsError"] is error
    assert call["toolCallId"] == "probe-call"
    assert call["detailsUndefined"] is True
    assert call["pendingTimers"] == 0
    assert call["abortListeners"] == 0
    # Every failure path is also a no-retry assertion.
    assert [request["method"] for request in seen] == ["GET", "POST"]
    return call


@pytest.mark.parametrize("status", ["ok", "error", "needs_confirmation", "ERROR", None])
def test_full_json_and_unicode_survive_pi_result_processing(
    agent_api: _AgentAPIServer, tmp_path: Path, status: str | None,
) -> None:
    name = "looper_probe-v2"
    _catalog(agent_api, name)
    arguments = {"文本": "你好 🌍\n\"quoted\"", "nested": {"a": [None, False, 0, 1.25]}, "empty": {}}
    answer = {"status": status, "data": {"完整": "消息🧪" * 5000}, "confirmation_id": "c-1"}
    agent_api.state.posts[name] = (200, answer)
    result, seen, stderr = _run(agent_api, tmp_path, binding=_binding(agent_api, slash=True),
                              calls=[_call(name, arguments=arguments)])
    call = _assert_call(result, seen, error=status == "error")
    assert call["content"] == [{"type": "text", "text": call["content"][0]["text"]}]
    assert json.loads(call["content"][0]["text"]) == answer
    assert seen[1] == {
        "method": "POST", "path": f"/api/agent/v1/ops/{name}",
        "authorization": f"Bearer {agent_api.state.token}", "content_type": "application/json", "body": arguments,
    }
    assert stderr == ""


@pytest.mark.parametrize(("status", "body", "fragment"), [
    (401, {"error": "invalid agent token"}, "HTTP 401"),
    (500, b"private raw response", "HTTP 500"),
    (201, {"status": "ok"}, "HTTP 201"),
    (200, b"private raw response", "JSON object"),
    (200, [1, 2], "JSON object"),
    (200, None, "JSON object"),
    (200, "text", "JSON object"),
])
def test_http_and_json_failures_are_pi_errors_without_retries(
    agent_api: _AgentAPIServer, tmp_path: Path, status: int, body: Any, fragment: str,
) -> None:
    _catalog(agent_api)
    agent_api.state.posts["looper_probe"] = (status, body)
    agent_api.state.post_headers = {"X-Private-Header": "never-print-this-header"}
    result, seen, _stderr = _run(agent_api, tmp_path, binding=_binding(agent_api), calls=[_call()])
    call = _assert_call(result, seen, error=True)
    assert fragment in call["content"][0]["text"]
    assert "private raw response" not in json.dumps(result)
    assert "never-print-this-header" not in json.dumps(result)


@pytest.mark.parametrize(("http_status", "status"), [
    (401, "error"), (200, "error"), (200, "ok"), (200, "needs_confirmation"),
])
def test_post_results_cannot_echo_credentials(
    agent_api: _AgentAPIServer, tmp_path: Path, http_status: int, status: str,
) -> None:
    _catalog(agent_api)
    agent_api.state.posts["looper_probe"] = (http_status, {
        "status": status, "error": f"rejected {agent_api.state.token}",
    })
    result, seen, _stderr = _run(agent_api, tmp_path, binding=_binding(agent_api), calls=[_call()])
    call = _assert_call(result, seen, error=status == "error")
    if http_status == 401:
        assert "HTTP 401" in call["content"][0]["text"]
    assert "[redacted]" in call["content"][0]["text"]


@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
def test_post_redirect_never_reaches_another_origin(
    agent_api: _AgentAPIServer, proxy: _Proxy, tmp_path: Path, status: int,
) -> None:
    _catalog(agent_api)
    agent_api.state.posts["looper_probe"] = (status, {})
    agent_api.state.post_headers = {"Location": f"http://127.0.0.1:{proxy.server_address[1]}/stolen"}
    result, seen, _stderr = _run(agent_api, tmp_path, binding=_binding(agent_api), calls=[_call()])
    call = _assert_call(result, seen, error=True)
    assert f"HTTP {status}" in call["content"][0]["text"]
    assert "/stolen" not in json.dumps(result)
    assert proxy.hits == 0


def test_post_bypasses_environment_proxies(
    agent_api: _AgentAPIServer, proxy: _Proxy, tmp_path: Path,
) -> None:
    _catalog(agent_api)
    agent_api.state.posts["looper_probe"] = (200, {"status": "ok"})
    proxy_url = f"http://127.0.0.1:{proxy.server_address[1]}"
    result, seen, _stderr = _run(agent_api, tmp_path, binding=_binding(agent_api), calls=[_call()], env_extra={
        "http_proxy": proxy_url, "HTTP_PROXY": proxy_url,
        "https_proxy": proxy_url, "HTTPS_PROXY": proxy_url,
        "all_proxy": proxy_url, "ALL_PROXY": proxy_url,
        "NO_PROXY": "", "no_proxy": "", "NODE_USE_ENV_PROXY": "1",
    })
    _assert_call(result, seen, error=False)
    assert proxy.hits == 0


@pytest.mark.parametrize(("name", "wait", "timeout"), [
    ("looper_probe", 300, 120_000),
    ("looper_room_inbox", 300, 320_000),
    ("looper_room_inbox", 100.5, 120_500),
    ("looper_room_inbox", 10, 120_000),
    ("looper_room_inbox", 0, 120_000),
    ("looper_room_inbox", -1, 120_000),
    ("looper_room_inbox", "300", 120_000),
    ("looper_room_inbox", True, 120_000),
    ("looper_room_inbox", None, 120_000),
])
def test_request_deadline_uses_only_positive_numeric_room_waits(
    agent_api: _AgentAPIServer, tmp_path: Path, name: str, wait: Any, timeout: int,
) -> None:
    _catalog(agent_api, name)
    agent_api.state.posts[name] = (200, {"messages": []})
    result, seen, _stderr = _run(agent_api, tmp_path, binding=_binding(agent_api),
                              calls=[_call(name, arguments={"wait_seconds": wait})])
    call = _assert_call(result, seen, error=False)
    assert call["timeouts"] == [timeout]


@pytest.mark.parametrize("control", ["abort", "timeout"])
@pytest.mark.parametrize("name", ["looper_probe", "looper_room_inbox"])
def test_cancel_and_deadline_close_the_request_and_warn_about_execution(
    agent_api: _AgentAPIServer, tmp_path: Path, control: str, name: str,
) -> None:
    _catalog(agent_api, name)
    agent_api.state.post_behavior = "hold"
    result, seen, _stderr = _run(agent_api, tmp_path, binding=_binding(agent_api),
                              calls=[_call(name, arguments={"wait_seconds": 300}, control=control)])
    call = _assert_call(result, seen, error=True)
    message = call["content"][0]["text"]
    assert "may still have run" in message
    assert "check its effect" in message
    if control == "abort":
        assert "cancelled" in message
    else:
        assert f"within {320 if name == 'looper_room_inbox' else 120} s" in message
    assert agent_api.state.disconnected.wait(1), "the bridge must close its HTTP connection"


@pytest.mark.parametrize("behavior", ["disconnect", "truncate", "malformed"])
def test_abnormal_connection_close_becomes_a_pi_error_once(
    agent_api: _AgentAPIServer, tmp_path: Path, behavior: str,
) -> None:
    _catalog(agent_api)
    agent_api.state.post_behavior = behavior
    result, seen, _stderr = _run(agent_api, tmp_path, binding=_binding(agent_api), calls=[_call()])
    _assert_call(result, seen, error=True)
    assert "never-print-this-header" not in json.dumps(result)


def test_harness_reads_pi_final_results_instead_of_only_execute_returns(tmp_path: Path) -> None:
    """Positive control for the real pi result processor, without HTTP or a model."""
    extension = tmp_path / "processor-probe.ts"
    extension.write_text('''export default function (pi) {
      pi.registerTool({ name: "probe", label: "probe", description: "processor probe",
        parameters: { type: "object", properties: { mode: { type: "string" } } },
        execute: async (_id, args) => {
          if (args.mode === "throw") throw new Error("processor control error");
          return { content: [{ type: "text", text: "processor control result" }],
            details: undefined, isError: args.mode === "error" };
        },
      });
    }''', encoding="utf-8")
    result, seen, stderr = _run(None, tmp_path, extension=extension, calls=[
        {"name": "probe", "arguments": {"mode": "ok"}},
        {"name": "probe", "arguments": {"mode": "error"}},
        {"name": "probe", "arguments": {"mode": "throw"}},
    ])
    assert seen == []
    assert stderr == ""
    assert result["loaded"] is True
    assert [call["ok"] for call in result["calls"]] == [True, True, True]
    assert [call["isError"] for call in result["calls"]] == [False, True, True]
    assert [call["executionIsError"] for call in result["calls"]] == [False, True, True]
    assert [call["content"][0]["text"] for call in result["calls"]] == [
        "processor control result", "processor control result", "processor control error",
    ]
