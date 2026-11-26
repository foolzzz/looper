"""A local scripted Anthropic Messages SSE endpoint, independent of any probe."""
from __future__ import annotations

from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading
from uuid import uuid4


def tool_use(name: str, input: dict) -> dict:
    return {"type": "tool_use", "id": "toolu_" + uuid4().hex, "name": name, "input": input}


def text(value: str) -> dict:
    return {"type": "text", "text": value}


class FakeAnthropicMessages:
    def start(self, script: list[dict]) -> tuple[str, list[dict]]:
        turns = deque(script)
        log: list[dict] = []
        lock = threading.Lock()

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def respond(self, status, body, content_type="application/json"):
                data = body.encode()
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                with lock:
                    log.append({"path": self.path, "model": None, "has_tools": False, "tools": None,
                                "system": None, "messages": [], "api_key": self.headers.get("x-api-key")})
                self.respond(404, json.dumps({"error": "unknown path"}))

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))))
                with lock:
                    log.append({"path": self.path, "model": body.get("model"),
                                "has_tools": "tools" in body, "tools": body.get("tools"),
                                "system": body.get("system"), "messages": body.get("messages", []),
                                "api_key": self.headers.get("x-api-key")})
                    path = self.path.split("?", 1)[0]
                    if path == "/v1/messages/count_tokens":
                        self.respond(200, json.dumps({"input_tokens": 1}))
                        return
                    if path != "/v1/messages":
                        self.respond(404, json.dumps({"error": "unknown path"}))
                        return
                    messages = body.get("messages", [])
                    # Some CLI versions append a system message after the user/tool result.
                    conversation = [message for message in messages if message.get("role") in ("user", "assistant")]
                    eligible = bool(conversation and conversation[-1]["role"] == "user")
                    scripted = "tools" in body and eligible
                    turn = dict(turns.popleft()) if scripted and turns else text("ok")
                model = body.get("model", "probe")
                message_id = "msg_" + uuid4().hex
                tool = turn["type"] == "tool_use"
                start = {**turn, "input": {}} if tool else {"type": "text", "text": ""}
                delta = {"type": "input_json_delta", "partial_json": json.dumps(turn["input"])} if tool else {
                    "type": "text_delta", "text": turn["text"]}
                events = [
                    {"type": "message_start", "message": {
                        "id": message_id, "type": "message", "role": "assistant", "model": model,
                        "content": [], "stop_reason": None, "stop_sequence": None,
                        "usage": {"input_tokens": 1, "output_tokens": 0}}},
                    {"type": "content_block_start", "index": 0, "content_block": start},
                    {"type": "content_block_delta", "index": 0, "delta": delta},
                    {"type": "content_block_stop", "index": 0},
                    {"type": "message_delta", "delta": {
                        "stop_reason": "tool_use" if tool else "end_turn", "stop_sequence": None},
                        "usage": {"output_tokens": 1}},
                    {"type": "message_stop"},
                ]
                if body.get("stream"):
                    self.respond(200, "".join(f"event: {event['type']}\ndata: {json.dumps(event)}\n\n"
                                               for event in events), "text/event-stream")
                else:
                    self.respond(200, json.dumps({**events[0]["message"], "content": [turn],
                        "stop_reason": "tool_use" if tool else "end_turn",
                        "usage": {"input_tokens": 1, "output_tokens": 1}}))

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        return f"http://127.0.0.1:{self.server.server_port}", log

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
