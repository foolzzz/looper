"""A local Lark platform for REST and long-connection tests."""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import Iterable
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

from websockets.asyncio.server import Server, ServerConnection, serve
from websockets.exceptions import ConnectionClosed

from looper.agents.channels.lark_stream import Frame


class FakeLarkPlatform:
    """Serve isolated platform responses on OS-assigned loopback ports."""

    def __init__(self, app_id: str = "cli_test", app_secret: str = "test-secret", *,
                 expire: int = 7200, ping_interval: float = 120) -> None:
        self.app_id = app_id
        self._app_secret = app_secret
        self.expire = expire
        self.ping_interval = ping_interval
        self.token_requests = 0
        self.sent: list[dict[str, Any]] = []
        self.requests: list[dict[str, Any]] = []
        self.bot: dict[str, str] = {"app_name": "Test Bot", "open_id": "ou_bot000001"}
        self.users: list[dict[str, Any]] = []
        self.departments: list[dict[str, Any] | str] = []
        self.acks: list[tuple[str | None, Any]] = []
        self.ack_frames: list[Frame] = []
        self.ping_frames: list[Frame] = []
        self.pings = 0
        self.connections = 0
        self.clients: set[ServerConnection] = set()
        self.respond_pings = True
        self._token: str | None = None
        self._recalled: set[str] = set()
        self._uuids: dict[str, str] = {}
        self._failures: list[tuple[str, int, str, int]] = []
        self._endpoint_error: tuple[int, str] | None = None
        self._http: asyncio.Server | None = None
        self._ws: Server | None = None
        self._base_url = ""
        self._ws_url = ""
        self._change = asyncio.Condition()
        self._event_count = 0

    @property
    def base_url(self) -> str:
        return self._base_url

    @property
    def ws_url(self) -> str:
        return self._ws_url

    async def __aenter__(self) -> FakeLarkPlatform:
        await self.start()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.stop()

    async def start(self) -> None:
        """Start both servers without using any external network."""
        self._ws = await serve(self._websocket, "127.0.0.1", 0, ping_interval=None)
        self._ws_url = f"ws://127.0.0.1:{self._ws.sockets[0].getsockname()[1]}/ws?device_id=test&service_id=7"
        self._http = await asyncio.start_server(self._request, "127.0.0.1", 0)
        self._base_url = f"http://127.0.0.1:{self._http.sockets[0].getsockname()[1]}"

    async def stop(self) -> None:
        """Close listeners and all outstanding WebSocket connections."""
        if self._http is not None:
            self._http.close()
            await self._http.wait_closed()
            self._http = None
        if self._ws is not None:
            self._ws.close()
            await self._ws.wait_closed()
            self._ws = None

    def invalidate_tokens(self) -> None:
        self._token = None

    def recall(self, message_id: str) -> None:
        self._recalled.add(message_id)

    def fail_next(self, path_prefix: str, code: int, msg: str, status: int = 400) -> None:
        self._failures.append((path_prefix, code, msg, status))

    def refuse_endpoint(self, code: int, msg: str) -> None:
        self._endpoint_error = (code, msg)

    async def _request(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            request = await reader.readuntil(b"\r\n\r\n")
            lines = request.decode("latin-1").split("\r\n")
            method, target, _ = lines[0].split(" ", 2)
            headers = {key.lower(): value.strip() for key, value in
                       (line.split(":", 1) for line in lines[1:] if line)}
            raw = await reader.readexactly(int(headers.get("content-length", "0")))
            body = json.loads(raw) if raw else None
            parsed = urlsplit(target)
            query = parse_qs(parsed.query, keep_blank_values=True)
            self.requests.append({"method": method, "path": parsed.path, "query": query,
                                  "body": body if parsed.path not in {
                                      "/open-apis/auth/v3/tenant_access_token/internal", "/callback/ws/endpoint"
                                  } else None,
                                  "headers": {key: value for key, value in headers.items()
                                              if key != "authorization"}})
            status, answer = self._answer(method, parsed.path, query, body, headers)
            payload = json.dumps(answer).encode()
            writer.write(f"HTTP/1.1 {status} Response\r\nContent-Type: application/json\r\n"
                         f"Content-Length: {len(payload)}\r\nConnection: close\r\n\r\n".encode() + payload)
            await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        finally:
            writer.close()
            with contextlib.suppress(ConnectionError):
                await writer.wait_closed()

    def _answer(self, method: str, path: str, query: dict[str, list[str]], body: Any,
                headers: dict[str, str]) -> tuple[int, dict[str, Any]]:
        for index, (prefix, code, msg, status) in enumerate(self._failures):
            if path.startswith(prefix):
                self._failures.pop(index)
                return status, {"code": code, "msg": msg}
        if path == "/open-apis/auth/v3/tenant_access_token/internal":
            self.token_requests += 1
            if body != {"app_id": self.app_id, "app_secret": self._app_secret}:
                return 400, {"code": 10003, "msg": "invalid param"}
            self._token = f"t-{self.token_requests}"
            return 200, {"code": 0, "tenant_access_token": self._token, "expire": self.expire}
        if path == "/callback/ws/endpoint":
            if body != {"AppID": self.app_id, "AppSecret": self._app_secret}:
                return 400, {"code": 10003, "msg": "invalid param"}
            if self._endpoint_error is not None:
                code, msg = self._endpoint_error
                self._endpoint_error = None
                return 400, {"code": code, "msg": msg}
            return 200, {"code": 0, "msg": "", "data": {"URL": self.ws_url, "ClientConfig": {
                "ReconnectCount": -1, "ReconnectInterval": 120, "ReconnectNonce": 30,
                "PingInterval": self.ping_interval}}}
        if self._token is None or headers.get("authorization") != f"Bearer {self._token}":
            return 400, {"code": 99991663, "msg": "Invalid access token for authorization"}
        if path == "/open-apis/bot/v3/info":
            return 200, {"code": 0, "bot": self.bot}
        if path.startswith("/open-apis/im/v1/messages"):
            return self._message(method, path, query, body)
        if path == "/open-apis/contact/v3/users/batch":
            wanted = query.get("user_ids", [])
            by_id = {user["open_id"]: user for user in self.users}
            if any(open_id not in by_id for open_id in wanted):
                return 400, {"code": 41050, "msg": "user not found"}
            return 200, {"code": 0, "data": {"items": [by_id[open_id] for open_id in wanted]}}
        if path == "/open-apis/contact/v3/users/batch_get_id":
            emails = body.get("emails", [])
            found = [{"user_id": user["open_id"], "email": user.get("email", "")}
                     for user in self.users if user.get("email") in emails
                     or user.get("enterprise_email") in emails]
            return 200, {"code": 0, "data": {"user_list": found}}
        if path == "/open-apis/contact/v3/departments/0/children":
            departments = [{"open_department_id": item} if isinstance(item, str) else item
                           for item in self.departments]
            return 200, {"code": 0, "data": self._page(departments, query)}
        if path == "/open-apis/contact/v3/users/find_by_department":
            department = query.get("department_id", ["0"])[0]
            users = [user for user in self.users if department in user.get("department_ids", ["0"])]
            return 200, {"code": 0, "data": self._page(users, query)}
        return 404, {"code": 404, "msg": "unknown endpoint"}

    @staticmethod
    def _page(items: list[dict[str, Any]], query: dict[str, list[str]]) -> dict[str, Any]:
        size = int(query.get("page_size", ["50"])[0])
        offset = int(query.get("page_token", ["0"])[0] or 0)
        end = offset + size
        return {"items": items[offset:end], "has_more": end < len(items),
                "page_token": str(end) if end < len(items) else ""}

    def _message(self, method: str, path: str, query: dict[str, list[str]],
                 body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        suffix = path.removeprefix("/open-apis/im/v1/messages")
        segments = suffix.strip("/").split("/")
        parent = unquote(segments[0]) if suffix else None
        if suffix.endswith("/reply") and parent in self._recalled:
            return 400, {"code": 230011, "msg": "The message is recalled."}
        key = body.get("uuid")
        if key is not None and key in self._uuids:
            return 200, {"code": 0, "data": {"message_id": self._uuids[key]}}
        kind = "send" if not suffix else "reply" if suffix.endswith("/reply") else (
            "edit" if method == "PUT" else "reaction")
        message_id = parent if kind in {"edit", "reaction"} else f"om_sent_{len(self.sent) + 1}"
        content = json.loads(body["content"]) if "content" in body else body.get("reaction_type")
        receive_type = query.get("receive_id_type", [None])[0]
        self.sent.append({"kind": kind, "chat": body.get("receive_id") if receive_type == "chat_id" else None,
                          "user": body.get("receive_id") if receive_type == "open_id" else None,
                          "parent": parent, "msg_type": body.get("msg_type"), "content": content,
                          "uuid": key, "message_id": message_id})
        if key is not None:
            self._uuids[key] = message_id
        return 200, {"code": 0, "data": {"message_id": message_id}}

    async def _websocket(self, connection: ServerConnection) -> None:
        self.clients.add(connection)
        self.connections += 1
        await self._notify()
        try:
            async for raw in connection:
                frame = Frame.decode(raw)
                if frame.method == 0 and frame.header("type") == "ping":
                    self.pings += 1
                    self.ping_frames.append(frame)
                    if self.respond_pings:
                        await connection.send(Frame(service=7, headers=[("type", "pong")]).encode())
                elif frame.method == 1:
                    self.ack_frames.append(frame)
                    self.acks.append((frame.header("message_id"), json.loads(frame.payload)))
                await self._notify()
        except ConnectionClosed:
            pass
        finally:
            self.clients.discard(connection)
            await self._notify()

    async def _notify(self) -> None:
        async with self._change:
            self._change.notify_all()

    async def wait_connections(self, n: int = 1, *, timeout: float = 2) -> None:
        async with asyncio.timeout(timeout):
            async with self._change:
                await self._change.wait_for(lambda: self.connections >= n and bool(self.clients))

    async def wait_acks(self, n: int, *, timeout: float = 2) -> None:
        async with asyncio.timeout(timeout):
            async with self._change:
                await self._change.wait_for(lambda: len(self.acks) >= n)

    async def push_frame(self, frame: Frame) -> None:
        await self.wait_connections()
        await asyncio.gather(*(client.send(frame.encode()) for client in tuple(self.clients)))

    async def push_event(self, event: dict[str, Any], *, message_id: str | None = None,
                         split: int = 1, frame_type: str = "event") -> None:
        self._event_count += 1
        message_id = message_id or f"frame_{self._event_count}"
        payload = json.dumps(event).encode()
        for seq in range(split):
            start, end = len(payload) * seq // split, len(payload) * (seq + 1) // split
            await self.push_frame(Frame(method=1, service=7, headers=[("type", frame_type),
                                  ("message_id", message_id), ("trace_id", "trace_test"),
                                  ("sum", str(split)), ("seq", str(seq))], payload=payload[start:end]))

    async def push_message(self, *, text: str, sender: str = "ou_sender", chat_type: str = "p2p",
                           chat_id: str = "oc_chat", message_id: str | None = None,
                           mentions: Iterable[dict[str, Any]] = (), parent_id: str | None = None,
                           root_id: str | None = None, event_id: str | None = None, split: int = 1) -> None:
        """Deliver a schema-2.0 message, optionally split across protocol frames."""
        message_id = message_id or f"om_fake_{self._event_count + 1}"
        event_id = event_id or f"ev_fake_{self._event_count + 1}"
        message = {"message_id": message_id, "chat_id": chat_id, "chat_type": chat_type,
                   "message_type": "text", "content": json.dumps({"text": text}), "mentions": list(mentions)}
        if parent_id:
            message["parent_id"] = parent_id
        if root_id:
            message["root_id"] = root_id
        await self.push_event({"schema": "2.0", "header": {"event_id": event_id,
                              "event_type": "im.message.receive_v1"}, "event": {
                                  "sender": {"sender_id": {"open_id": sender}, "sender_type": "user"},
                                  "message": message}}, message_id=message_id, split=split)

    async def drop_connections(self) -> None:
        await asyncio.gather(*(client.close(code=1011) for client in tuple(self.clients)))
