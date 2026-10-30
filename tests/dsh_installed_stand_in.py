"""Local synthetic Messages/MCP peers. These are protocol fixtures, never model evals."""

from __future__ import annotations

import json
import os
import sys
import threading
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


def read_records(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def tool(name: str, arguments: dict) -> dict:
    return {"type": "tool_use", "name": name, "input": arguments}


@dataclass
class Reply:
    blocks: list[str | dict] = field(default_factory=list)
    usage: dict[str, int] = field(default_factory=lambda: {
        "input": 11, "output": 5, "cache_read": 0, "cache_write": 0})
    status: int = 200
    cut: bool = False
    hold: threading.Event | None = None
    eof_delay: float = 0
    error_message: str = "Synthetic provider rejection"
    error_type: str | None = None
    headers: dict[str, str] = field(default_factory=dict)
    disconnect: bool = False


def messages_wire(blocks: list[str | dict] | Reply, index: int) -> str:
    reply = blocks if isinstance(blocks, Reply) else Reply(blocks)
    blocks, usage = reply.blocks, reply.usage
    events = [{"type": "message_start", "message": {
        "id": f"synthetic-{index}", "type": "message", "role": "assistant",
        "model": "deepseek-flash", "content": [], "usage": {
            "input_tokens": usage.get("input", 11), "output_tokens": 0,
            "cache_read_input_tokens": usage.get("cache_read", 0),
            "cache_creation_input_tokens": usage.get("cache_write", 0)}}}]
    for number, block in enumerate(blocks):
        content = ({"type": "text", "text": block} if isinstance(block, str)
                   else {**block, "id": f"synthetic-tool-{index}-{number}", "input": {}})
        events.append({"type": "content_block_start", "index": number, "content_block": content})
        if isinstance(block, dict):
            events.append({"type": "content_block_delta", "index": number,
                           "delta": {"type": "input_json_delta", "partial_json": json.dumps(block["input"])}})
        events.append({"type": "content_block_stop", "index": number})
    events.extend([{"type": "message_delta", "delta": {
        "stop_reason": "tool_use" if any(isinstance(block, dict) for block in blocks) else "end_turn"},
        "usage": {"output_tokens": usage.get("output", 5)}}, {"type": "message_stop"}])
    return "".join(f"event: {event['type']}\ndata: {json.dumps(event)}\n\n" for event in events)


class FetchMessages:
    """Explicit in-memory fetch seam, separate from the HTTP listener verification."""

    def __init__(self, replies, env):
        self.env = env
        self.log = Path(env["LOCAL_NETWORK_LOG"])
        self.responses = self.log.with_name("synthetic-responses.json")
        self.responses.write_text(json.dumps([messages_wire(blocks, index) for index, blocks in enumerate(replies)]))
        self.port = 1
        self.url = "http://127.0.0.1:1"

    @property
    def requests(self):
        return [row["request"] for row in read_records(self.log) if row["kind"] == "fetch"]

    def __enter__(self):
        self.env["LOCAL_MESSAGES_RESPONSES"] = str(self.responses)
        return self

    def __exit__(self, *args):
        self.env.pop("LOCAL_MESSAGES_RESPONSES", None)


class MessagesServer:
    """Bounded scripted SSE replies; unexpected requests are counted and fail visibly."""

    def __init__(self, replies: list[list[str | dict] | Reply]):
        self.requests: list[dict] = []
        self.replies = replies
        self.received = threading.Event()
        self._lock = threading.Lock()
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))) or b"{}")
                with owner._lock:
                    index = len(owner.requests)
                    owner.requests.append({"path": self.path, "body": body, "at": time.monotonic_ns(),
                                           "api_key": self.headers.get("x-api-key"),
                                           "request_id": self.headers.get("x-looper-request-id")})
                owner.received.set()
                if self.path != "/v1/messages" or index >= len(owner.replies):
                    self.send_error(409, "Unexpected local request")
                    return
                reply = owner.replies[index]
                reply = reply if isinstance(reply, Reply) else Reply(reply)
                if reply.disconnect:
                    self.close_connection = True
                    return
                if reply.hold is not None and not reply.hold.wait(30):
                    return
                if reply.status != 200:
                    wire = json.dumps({"type": "error", "error": {
                        "type": reply.error_type or ("authentication_error" if reply.status in (401, 403) else "api_error"),
                        "message": reply.error_message}}).encode()
                else:
                    wire = messages_wire(reply, index).encode()
                self.send_response(reply.status)
                self.send_header("Content-Type", "text/event-stream" if reply.status == 200 else "application/json")
                # A trailing SSE comment separates message_stop from HTTP EOF.
                tail = b": eof\n\n" if reply.eof_delay else b""
                self.send_header("Content-Length", str(len(wire) + len(tail)))
                for name, value in reply.headers.items():
                    self.send_header(name, value)
                self.end_headers()
                try:
                    self.wfile.write((wire.split(b"event: message_stop")[0] if reply.status == 200 else wire[:-1])
                                     if reply.cut else wire)
                    self.wfile.flush()
                    if tail and not reply.cut:
                        time.sleep(reply.eof_delay)
                        self.wfile.write(tail)
                        self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    pass
                if reply.cut:
                    self.close_connection = True

        self.http = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.http.serve_forever, daemon=True)
        self.port = self.http.server_port
        self.url = f"http://127.0.0.1:{self.port}"

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *args):
        for reply in self.replies:
            if isinstance(reply, Reply) and reply.hold is not None:
                reply.hold.set()
        self.http.shutdown()
        self.http.server_close()
        self.thread.join(timeout=3)


def mcp_command(log: Path, names: list[str], *, gate: Path | None = None, mode: str = "ready") -> list[str]:
    return [sys.executable, str(Path(__file__).resolve()), str(log), json.dumps(names),
            str(gate) if gate else "", mode]


def _mcp_main():
    log, names, gate, mode = Path(sys.argv[1]), json.loads(sys.argv[2]), sys.argv[3], sys.argv[4]
    for line in sys.stdin:
        request = json.loads(line)
        row = {"method": request["method"], "params": request.get("params", {}),
               "at": time.monotonic_ns(), "pid": os.getpid()}
        with log.open("a") as output:
            output.write(json.dumps(row) + "\n")
        if "id" not in request:
            continue
        method = request["method"]
        if method == "initialize":
            result = {"protocolVersion": "2024-11-05", "capabilities": {"tools": {}},
                      "serverInfo": {"name": "synthetic-looper", "version": "1"}}
        elif method == "tools/list":
            if mode == "crash":
                raise SystemExit(7)
            if gate:
                deadline = time.monotonic() + 10
                while not Path(gate).exists():
                    if time.monotonic() >= deadline:
                        raise SystemExit(8)
                    threading.Event().wait(.02)
            result = {"tools": [{"name": name, "description": "Disposable platform probe",
                                  "inputSchema": {"type": "object", "properties": {}}} for name in names]}
        elif method == "tools/call":
            if request["params"]["name"] not in names:
                raise SystemExit(9)
            result = {"content": [{"type": "text", "text": "platform called"}]}
        else:
            print(json.dumps({"jsonrpc": "2.0", "id": request["id"],
                              "error": {"code": -32601, "message": "Unsupported fixture method"}}), flush=True)
            continue
        print(json.dumps({"jsonrpc": "2.0", "id": request["id"], "result": result}), flush=True)


# Loaded before any dsh module. Record attempted network activity and refuse every
# TCP destination except this test's Messages server; a disabled upload cannot escape.
NETWORK_GUARD = r'''
import {appendFileSync, readFileSync} from 'node:fs';
import net from 'node:net';
import dns from 'node:dns';
import {syncBuiltinESMExports} from 'node:module';
const log = row => appendFileSync(process.env.LOCAL_NETWORK_LOG, JSON.stringify(row) + '\n');
const allowed = (host, port) => host === '127.0.0.1' && Number(port) === Number(process.env.LOCAL_MESSAGES_PORT);
const original = net.Socket.prototype.connect;
net.Socket.prototype.connect = function(...args) {
  let value = args[0];
  if (Array.isArray(value)) value = value[0];
  const host = typeof value === 'object' ? value.host ?? 'localhost' : typeof args[1] === 'string' ? args[1] : 'localhost';
  const port = typeof value === 'object' ? value.port : value;
  log({kind: 'connect', host, port, allowed: allowed(host, port)});
  if (!allowed(host, port)) throw Error('Offline DSH test refused network destination');
  return original.apply(this, args);
};
const lookup = dns.lookup;
dns.lookup = function(host, ...args) {
  log({kind: 'dns', host});
  if (host !== '127.0.0.1') throw Error('Offline DSH test refused DNS lookup');
  return lookup.call(this, host, ...args);
};
const fetch = globalThis.fetch;
globalThis.fetch = function(input, options) {
  const url = new URL(typeof input === 'string' || input instanceof URL ? input : input.url);
  const fixture = process.env.LOCAL_MESSAGES_RESPONSES;
  log({kind: 'fetch', url: url.href, method: options?.method ?? 'GET', ...(fixture ? {request: {
    path: url.pathname, body: options?.body ? JSON.parse(options.body) : null,
    api_key: new Headers(options?.headers).get('x-api-key'),
  }} : {})});
  if (!allowed(url.hostname, url.port)) throw Error('Offline DSH test refused fetch destination');
  if (fixture) {
    const index = readFileSync(process.env.LOCAL_NETWORK_LOG, 'utf8').trim().split('\n')
      .map(line => JSON.parse(line)).filter(row => row.kind === 'fetch').length - 1;
    const wire = JSON.parse(readFileSync(fixture, 'utf8'))[index];
    if (url.pathname !== '/v1/messages' || wire === undefined) throw Error('Unexpected synthetic Messages request');
    return Promise.resolve(new Response(wire, {status: 200, headers: {'Content-Type': 'text/event-stream'}}));
  }
  return fetch(input, options);
};
syncBuiltinESMExports();
'''


if __name__ == "__main__":
    _mcp_main()
