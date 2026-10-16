"""``looper mcp-server``: Looper's platform tools for the Manager, or for one task's Planner, as an MCP server on
stdin/stdout.

The agent's runtime starts this process with ``LOOPER_AGENT_URL`` (the ``looper serve`` that runs the agent) and
``LOOPER_AGENT_TOKEN``: the Manager's token, or a Planner's for its task, which decides the tools it lists. It only
forwards: every validation, confirmation and execution happens in ``looper serve``.

- **Transport.** MCP stdio: one JSON-RPC 2.0 message per line on stdin, one compact JSON line per response on
  stdout. Nothing else is written to stdout; logs go to stderr. EOF on stdin ends the process with 0.
- **Tools.** ``tools/list`` answers the tools of ``GET <url>/api/agent/v1/tools``. ``tools/call`` posts the
  arguments to ``POST <url>/api/agent/v1/ops/<name>`` and answers its JSON body as the tool result's text, an
  error result when the body's ``status`` is ``"error"`` (``"ok"`` and ``"needs_confirmation"`` are not).
- **Failures.** Both requests send ``Authorization: Bearer <token>``; anything but a 200 with a JSON object,
  or no answer within 120 s, is a failure. A failed ``tools/list`` is a JSON-RPC error; a failed ``tools/call``
  is an error tool result that tells the model why, since it can read that and decide what to do.
"""

from __future__ import annotations

import http.client
import json
import logging
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, BinaryIO, Protocol

from looper import __version__

logger = logging.getLogger(__name__)

DEFAULT_PROTOCOL_VERSION = "2025-06-18"
REQUEST_TIMEOUT_SECONDS = 120.0

# JSON-RPC 2.0 error codes.
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603


class LooperUnavailable(Exception):
    """Looper gave no usable answer; the message says why (never the token)."""


class LooperTimedOut(LooperUnavailable):
    """Looper did not answer in time, so a posted tool call may still have run."""


class Client(Protocol):
    """What ``handle`` calls: AgentClient, or a stand-in in tests."""

    def list_tools(self) -> list[Any]: ...

    def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]: ...


class AgentClient:
    """The two requests to ``looper serve``'s agent API."""

    def __init__(self, url: str, token: str, *, timeout: float = REQUEST_TIMEOUT_SECONDS) -> None:
        self.url = url.rstrip("/")
        self._token = token
        self._timeout = timeout
        # The URL is the local server that started the Manager; a proxy from the environment must not carry it.
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def list_tools(self) -> list[Any]:
        """The ``tools`` list of ``GET /api/agent/v1/tools``; raises LooperUnavailable."""
        tools = self._request("/api/agent/v1/tools").get("tools")
        if not isinstance(tools, list):
            raise LooperUnavailable("its answer has no tools list")
        return tools

    def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """The JSON object ``POST /api/agent/v1/ops/<name>`` answers; raises LooperUnavailable."""
        return self._request(f"/api/agent/v1/ops/{urllib.parse.quote(name, safe='')}", body=arguments)

    def _request(self, path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
        headers = {"Authorization": f"Bearer {self._token}", "Accept": "application/json"}
        data = None
        if body is not None:
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(self.url + path, data=data, headers=headers,
                                         method="GET" if body is None else "POST")
        try:
            with self._opener.open(request, timeout=self._timeout) as response:
                status, raw = response.status, response.read()
        except urllib.error.HTTPError as exc:  # a non-2xx answer
            status, raw = exc.code, exc.read()
        except (OSError, http.client.HTTPException) as exc:
            reason = exc.reason if isinstance(exc, urllib.error.URLError) else exc
            if isinstance(reason, TimeoutError):
                raise LooperTimedOut(f"no answer from {self.url} within {self._timeout:g} s") from exc
            raise LooperUnavailable(f"no answer from {self.url} ({reason})") from exc
        try:
            payload = json.loads(raw)
        except ValueError:
            payload = None
        if status != 200:
            error = payload.get("error") if isinstance(payload, dict) else None
            raise LooperUnavailable(f"HTTP {status}" + (f": {error}" if isinstance(error, str) and error else ""))
        if not isinstance(payload, dict):
            raise LooperUnavailable("its answer is not a JSON object")
        return payload


def handle(message: dict[str, Any], client: Client) -> dict[str, Any] | None:
    """The JSON-RPC response to one message, or None when it needs none (a notification or a response)."""
    if "id" not in message:
        return None
    message_id = message["id"]
    method = message.get("method")
    if not isinstance(method, str):
        if "result" in message or "error" in message:
            return None  # a response; this server sends no requests
        return _error(message_id, INVALID_REQUEST, "invalid request: method must be a string")
    params = message.get("params")
    params = params if isinstance(params, dict) else {}
    if method == "initialize":
        version = params.get("protocolVersion")
        return _result(message_id, {
            "protocolVersion": version if isinstance(version, str) else DEFAULT_PROTOCOL_VERSION,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": "looper", "version": __version__},
        })
    if method == "ping":
        return _result(message_id, {})
    if method == "tools/list":
        try:
            tools = client.list_tools()
        except LooperUnavailable as exc:
            logger.warning("tools/list failed: %s", exc)
            return _error(message_id, INTERNAL_ERROR, f"Looper did not answer the tools list: {exc}")
        return _result(message_id, {"tools": tools})
    if method == "tools/call":
        return _call_tool(message_id, params, client)
    return _error(message_id, METHOD_NOT_FOUND, f"method not found: {method}")


def _call_tool(message_id: Any, params: dict[str, Any], client: Client) -> dict[str, Any]:
    name = params.get("name")
    if not isinstance(name, str) or not name:
        return _error(message_id, INVALID_PARAMS, "tools/call needs params.name, the tool's name")
    arguments = params.get("arguments")
    if arguments is None:
        arguments = {}
    if not isinstance(arguments, dict):
        return _error(message_id, INVALID_PARAMS, "tools/call params.arguments must be an object")
    try:
        answer = client.call_tool(name, arguments)
    except LooperTimedOut as exc:
        logger.warning("tools/call %s: %s", name, exc)
        return _tool_result(message_id, f"Looper did not answer the call to {name}: {exc}. It may still have run, "
                                        "so check its effect before calling it again.", is_error=True)
    except LooperUnavailable as exc:
        logger.warning("tools/call %s: %s", name, exc)
        return _tool_result(message_id, f"Looper did not run the tool {name}: {exc}", is_error=True)
    status = answer.get("status")
    logger.info("tools/call %s: %s", name, status)
    return _tool_result(message_id, json.dumps(answer, ensure_ascii=False), is_error=status == "error")


def handle_line(line: str | bytes, client: Client) -> dict[str, Any] | None:
    """The response to one line of stdin (UTF-8); a line that is not JSON, or not an object, gets ``"id": null``."""
    try:
        message = json.loads(line)
    except ValueError as exc:
        return _error(None, PARSE_ERROR, f"parse error: {exc}")
    if not isinstance(message, dict):
        return _error(None, INVALID_REQUEST, "invalid request: a message must be a JSON object")
    try:
        return handle(message, client)
    except Exception as exc:  # a bug here must not end the Manager's tools
        logger.exception("handling %s failed", message.get("method"))
        return _error(message["id"], INTERNAL_ERROR, f"internal error: {exc}") if "id" in message else None


def serve(client: Client, stdin: BinaryIO, stdout: BinaryIO) -> int:
    """Answer each line of ``stdin`` on ``stdout`` until EOF; returns the exit code."""
    for line in stdin:
        if not line.strip():
            continue
        response = handle_line(line, client)
        if response is None:
            continue
        try:
            stdout.write(json.dumps(response, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n")
            stdout.flush()
        except BrokenPipeError:
            return 0  # the client is gone
    return 0


def main() -> int:
    """Serve the Manager on stdin/stdout until EOF; 2 when LOOPER_AGENT_URL or LOOPER_AGENT_TOKEN is not usable."""
    logging.basicConfig(level=logging.INFO, stream=sys.stderr, format="looper mcp-server %(levelname)s %(message)s")
    url = os.environ.get("LOOPER_AGENT_URL", "").strip()
    token = os.environ.get("LOOPER_AGENT_TOKEN", "").strip()
    missing = [name for name, value in (("LOOPER_AGENT_URL", url), ("LOOPER_AGENT_TOKEN", token)) if not value]
    if missing:
        print(f"error: looper mcp-server needs {' and '.join(missing)}; the Manager's runtime sets them when it "
              "starts this server", file=sys.stderr)
        return 2
    if urllib.parse.urlsplit(url).scheme not in ("http", "https"):
        print(f"error: LOOPER_AGENT_URL must be an http:// or https:// URL, not {url!r}", file=sys.stderr)
        return 2
    client = AgentClient(url, token)
    logger.info("forwarding tool calls to %s", client.url)
    return serve(client, sys.stdin.buffer, sys.stdout.buffer)


def _result(message_id: Any, result: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": message_id, "result": result}


def _error(message_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": message_id, "error": {"code": code, "message": message}}


def _tool_result(message_id: Any, text: str, *, is_error: bool) -> dict[str, Any]:
    return _result(message_id, {"content": [{"type": "text", "text": text}], "isError": is_error})
