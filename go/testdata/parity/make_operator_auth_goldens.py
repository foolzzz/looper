#!/usr/bin/env python3
"""Capture operator-auth HTTP responses from this checkout's Python server.

Each case is one AgentHTTPServer._handle_connection exchange. The static
files, port, and tokens are fixed so Set-Cookie and the login page compare
byte for byte with the Go server. No database and no real channel.

GET /api/agent/v1/tools with a valid Manager token is the exception. Python
lists that service's tools. The Go port keeps the route 404 until the
service exists (docs/plans/loopgo.md). Those bytes are still Python
_json_response, the same 404 the other agent routes return, and the case
is marked deferred_tools. A token that would not pass ManagerOps.authorized
must not use that mark.

Usage (repository root): python go/testdata/parity/make_operator_auth_goldens.py
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
from pathlib import Path
import sys
import tempfile

import make_goldens as goldens

sys.path.insert(0, str(goldens.REPO))

HERE = Path(__file__).resolve().parent / "operator_auth"
PORT = 18834
OPERATOR = "operator_" + "a" * 32
AGENT = "agent_" + "b" * 32
FILES = {
    "index.html": "<html>dashboard</html>",
    "assets/app.js": "console.log(1)",
    "docs/index.html": "DOC",
}


class BoundaryOps:
    """The Manager token check, without a tool catalog."""

    def __init__(self, token: str) -> None:
        self.token = token

    def authorized(self, header: str | None) -> bool:
        scheme, _, value = (header or "").partition(" ")
        digest = value.strip().encode()
        if scheme.lower() != "bearer" or len(digest) != len(self.token):
            return False
        return hmac.compare_digest(digest, self.token.encode())

    def refuse(self, path: str) -> None:
        return None

    def listing(self) -> list:
        raise AssertionError("tool listing is deferred; this case must set deferred_tools")


class MemoryWriter:
    def __init__(self, peer: tuple[str, int]) -> None:
        self.peer = peer
        self.buf = bytearray()

    def write(self, data: bytes) -> None:
        self.buf.extend(data)

    def get_extra_info(self, name: str):
        if name == "peername":
            return self.peer
        return None

    def close(self) -> None:
        pass

    async def drain(self) -> None:
        pass

    async def wait_closed(self) -> None:
        pass


def cookie(token: str, port: int = PORT) -> str:
    return f"other=value; looper_operator_{port}={token}"


def case(name: str, method: str, target: str, **extra) -> dict:
    row = {"name": name, "method": method, "target": target, "headers": {}, "source": "handler"}
    row.update(extra)
    return row


def matrix() -> list[dict]:
    bearer = {"Authorization": "Bearer " + OPERATOR}
    agent = {"Authorization": "Bearer " + AGENT}
    navigate = {"Sec-Fetch-Mode": "navigate", "Sec-Fetch-Dest": "document"}
    return [
        case("missing", "GET", "/api/nothing-here"),
        case("wrong", "GET", "/api/nothing-here", headers={"Authorization": "Bearer wrong"}),
        case("bearer-unknown", "GET", "/api/nothing-here", headers=bearer),
        case("cookie-unknown", "GET", "/api/nothing-here", headers={"Cookie": cookie(OPERATOR)}),
        case("other-port", "GET", "/api/nothing-here",
             headers={"Cookie": cookie(OPERATOR, PORT + 1)}),
        case("agent-on-operator", "GET", "/api/nothing-here", headers=agent),
        case("agent-and-cookie", "GET", "/api/nothing-here",
             headers={"Authorization": "Bearer " + AGENT, "Cookie": cookie(OPERATOR)}),
        case("mixed-case-bearer", "GET", "/api/nothing-here",
             headers={"Authorization": "bEaReR " + OPERATOR}),
        case("put-missing", "PUT", "/api/tasks/task-1/roles/coder"),
        case("options-api-missing", "OPTIONS", "/api/tasks/create"),
        case("options-api-token", "OPTIONS", "/api/tasks/create", headers=bearer),
        case("post-import-missing", "POST", "/api/config/imports"),
        case("agent-tools", "GET", "/api/agent/v1/tools", headers=agent,
             source="deferred-tools", deferred_tools=True),
        case("operator-on-agent", "GET", "/api/agent/v1/tools", headers=bearer),
        case("agent-missing", "GET", "/api/agent/v1/tools"),
        case("options-agent-missing", "OPTIONS", "/api/agent/v1/tools"),
        case("options-agent-token", "OPTIONS", "/api/agent/v1/tools", headers=agent),
        case("agent-scheme", "GET", "/api/agent/v1/tools",
             headers={"Authorization": "bearer " + AGENT},
             source="deferred-tools", deferred_tools=True),
        case("agent-spaced", "GET", "/api/agent/v1/nope",
             headers={"Authorization": "Bearer  " + AGENT + "  "}),
        case("agent-prefix", "GET", "/api/agent/v1"),
        case("agent-dotdot", "GET", "/api/agent/v1/../status", headers=agent),
        case("root-navigate", "GET", "/", headers=navigate),
        case("index-navigate", "GET", "/index.html", headers=navigate),
        case("spa-navigate", "GET", "/tasks/task-1", headers=navigate),
        case("assets-dir-navigate", "GET", "/assets", headers=navigate),
        case("no-fetch", "GET", "/"),
        case("iframe", "GET", "/", headers={"Sec-Fetch-Mode": "navigate", "Sec-Fetch-Dest": "iframe"}),
        case("script", "GET", "/assets/app.js",
             headers={"Sec-Fetch-Mode": "navigate", "Sec-Fetch-Dest": "script"}),
        case("docs-index", "GET", "/docs/index.html", headers=navigate),
        case("xff", "GET", "/", headers={**navigate, "X-Forwarded-For": ""}),
        case("forwarded-empty", "GET", "/", headers={**navigate, "Forwarded": ""}),
        case("bearer-not-browser", "GET", "/", headers=bearer),
        case("query-token", "GET", "/?token=" + OPERATOR),
        case("query-code", "GET", "/?code=" + OPERATOR),
        case("login-navigate", "GET", "/login", headers=navigate),
        case("login-cookie", "GET", "/login", headers={"Cookie": f"looper_operator_{PORT}={OPERATOR}"}),
        case("login-anonymous", "GET", "/login"),
        case("options-dashboard", "OPTIONS", "/"),
        case("empty-token-api", "GET", "/api/nothing-here",
             headers=bearer, clear_operator_token=True),
        case("empty-token-navigate", "GET", "/", headers=navigate, clear_operator_token=True),
        case("remote-not-loopback", "GET", "/", headers=navigate, remote="10.0.0.5:12345"),
        case("bad-host", "GET", "/", headers=navigate, host="evil.example:18834"),
    ]


def request_bytes(row: dict) -> bytes:
    host = row.get("host") or f"127.0.0.1:{PORT}"
    lines = [f"{row['method']} {row['target']} HTTP/1.1", f"Host: {host}"]
    for key, value in row.get("headers", {}).items():
        lines.append(f"{key}: {value}")
    return ("\r\n".join(lines) + "\r\n\r\n").encode()


def peer_host(row: dict) -> str:
    remote = row.get("remote") or "127.0.0.1:12345"
    host, _, _ = remote.rpartition(":")
    return host or remote


def deferred_tools(row: dict) -> bytes:
    from looper.agents.http_server import HTTP_404, _json_response

    header = row.get("headers", {}).get("Authorization")
    if not BoundaryOps(AGENT).authorized(header):
        raise AssertionError(f"{row['name']} is not an authorized tools request")
    path = row["target"].split("?", 1)[0]
    return _json_response(HTTP_404, {"error": f"no {row['method']} {path}"})


async def capture(server, row: dict) -> bytes:
    if row.get("deferred_tools"):
        return deferred_tools(row)
    reader = asyncio.StreamReader()
    reader.feed_data(request_bytes(row))
    reader.feed_eof()
    writer = MemoryWriter((peer_host(row), 12345))
    server._operator_token = None if row.get("clear_operator_token") else OPERATOR
    await server._handle_connection(reader, writer)
    if not writer.buf.startswith(b"HTTP/1.1 "):
        raise AssertionError(f"{row['name']} produced no status line")
    return bytes(writer.buf)


async def main() -> None:
    from looper.agents.http_server import AgentHTTPServer
    from types import SimpleNamespace

    rows = matrix()
    names = [row["name"] for row in rows]
    if len(names) != len(set(names)):
        raise AssertionError("duplicate case name")
    if hashlib.sha256(OPERATOR.encode()).hexdigest() == hashlib.sha256(AGENT.encode()).hexdigest():
        raise AssertionError("tokens collided")
    HERE.mkdir(parents=True)
    with tempfile.TemporaryDirectory(prefix="loopgo-auth-golden-") as temp:
        root = Path(temp)
        for name, body in FILES.items():
            path = root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(body)
        server = AgentHTTPServer(
            port=PORT, operator_token=OPERATOR, static_dir=root, runtime_root=root,
            web_adapter=SimpleNamespace(), task_router=SimpleNamespace(),
            supervisor=SimpleNamespace(), manager_ops=BoundaryOps(AGENT),
        )
        outputs = {}
        for row in rows:
            outputs[row["name"]] = await capture(server, row)
    manifest = {
        "port": PORT,
        "operator_token": OPERATOR,
        "agent_token": AGENT,
        "files": FILES,
        "cases": rows,
    }
    (HERE / "cases.json").write_text(json.dumps(manifest, indent=2) + "\n")
    keep = {name + ".http" for name in names}
    keep.add("cases.json")
    for path in HERE.iterdir():
        if path.name not in keep:
            path.unlink()
    for name, data in outputs.items():
        (HERE / f"{name}.http").write_bytes(data)
    print(f"Wrote {len(outputs)} operator-auth goldens.")


if __name__ == "__main__":
    asyncio.run(main())
