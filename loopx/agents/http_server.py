"""Async HTTP server for the LoopX agent system.

Pure stdlib asyncio — zero external dependencies. Serves:
  - REST API endpoints (/api/*)
  - SSE streaming (/api/chat/events)
  - Static files from web/dist/ with SPA fallback
"""

from __future__ import annotations

import asyncio
import json
import logging
import mimetypes
import urllib.parse
from pathlib import Path
from typing import Any

from .channels.web import WebChannelAdapter
from .supervisor import AgentSupervisor
from .tiers.task import TaskRouter

logger = logging.getLogger(__name__)

_STATIC_DIR = Path(__file__).parent.parent.parent / "web" / "dist"

HTTP_200 = "200 OK"
HTTP_400 = "400 Bad Request"
HTTP_404 = "404 Not Found"
HTTP_405 = "405 Method Not Allowed"
HTTP_500 = "500 Internal Server Error"


def _json_response(status: str, body: Any) -> bytes:
    payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
    headers = (
        f"HTTP/1.1 {status}\r\n"
        f"Content-Type: application/json; charset=utf-8\r\n"
        f"Content-Length: {len(payload)}\r\n"
        f"Access-Control-Allow-Origin: *\r\n"
        f"Access-Control-Allow-Methods: GET, POST, OPTIONS\r\n"
        f"Access-Control-Allow-Headers: Content-Type\r\n"
        f"\r\n"
    )
    return headers.encode("utf-8") + payload


def _cors_preflight() -> bytes:
    return (
        "HTTP/1.1 204 No Content\r\n"
        "Access-Control-Allow-Origin: *\r\n"
        "Access-Control-Allow-Methods: GET, POST, OPTIONS\r\n"
        "Access-Control-Allow-Headers: Content-Type\r\n"
        "Access-Control-Max-Age: 86400\r\n"
        "\r\n"
    ).encode("utf-8")


class AgentHTTPServer:
    """Async HTTP server bridging REST/SSE to the agent system."""

    def __init__(
        self,
        *,
        host: str = "127.0.0.1",
        port: int = 8767,
        web_adapter: WebChannelAdapter,
        task_router: TaskRouter,
        supervisor: AgentSupervisor,
        static_dir: Path | None = None,
    ) -> None:
        self._host = host
        self._port = port
        self._web_adapter = web_adapter
        self._task_router = task_router
        self._supervisor = supervisor
        self._static_dir = static_dir or _STATIC_DIR
        self._server: asyncio.Server | None = None

    async def start(self) -> None:
        self._server = await asyncio.start_server(
            self._handle_connection, self._host, self._port
        )
        logger.info("HTTP server listening on %s:%d", self._host, self._port)

    async def stop(self) -> None:
        if self._server:
            self._server.close()
            await self._server.wait_closed()
            logger.info("HTTP server stopped")

    async def _handle_connection(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        try:
            request_line = await asyncio.wait_for(reader.readline(), timeout=30.0)
            if not request_line:
                writer.close()
                return

            request_str = request_line.decode("utf-8", errors="replace").strip()
            parts = request_str.split(" ")
            if len(parts) < 2:
                writer.write(_json_response(HTTP_400, {"error": "bad request"}))
                await writer.drain()
                writer.close()
                return

            method = parts[0].upper()
            raw_path = parts[1]

            headers = await self._read_headers(reader)
            body = b""
            content_length = int(headers.get("content-length", "0"))
            if content_length > 0:
                body = await asyncio.wait_for(
                    reader.readexactly(content_length), timeout=30.0
                )

            parsed = urllib.parse.urlparse(raw_path)
            path = parsed.path
            query = dict(urllib.parse.parse_qsl(parsed.query))

            if method == "OPTIONS":
                writer.write(_cors_preflight())
                await writer.drain()
                writer.close()
                return

            if path.startswith("/api/"):
                await self._route_api(method, path, query, body, writer)
            else:
                await self._serve_static(path, writer)

        except (asyncio.TimeoutError, ConnectionError, asyncio.IncompleteReadError):
            pass
        except Exception:
            logger.exception("error handling HTTP request")
            try:
                writer.write(_json_response(HTTP_500, {"error": "internal server error"}))
                await writer.drain()
            except Exception:
                pass
        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass

    @staticmethod
    async def _read_headers(reader: asyncio.StreamReader) -> dict[str, str]:
        headers: dict[str, str] = {}
        while True:
            line = await asyncio.wait_for(reader.readline(), timeout=10.0)
            decoded = line.decode("utf-8", errors="replace").strip()
            if not decoded:
                break
            if ":" in decoded:
                key, _, value = decoded.partition(":")
                headers[key.strip().lower()] = value.strip()
        return headers

    # --- API routing ---

    async def _route_api(
        self,
        method: str,
        path: str,
        query: dict[str, str],
        body: bytes,
        writer: asyncio.StreamWriter,
    ) -> None:
        if path == "/api/status" and method == "GET":
            await self._handle_status(writer)
        elif path == "/api/tasks" and method == "GET":
            await self._handle_list_tasks(writer)
        elif path == "/api/chat/send" and method == "POST":
            await self._handle_chat_send(body, writer)
        elif path == "/api/chat/events" and method == "GET":
            await self._handle_sse_events(query, writer)
        elif path == "/api/tasks/cancel" and method == "POST":
            await self._handle_task_cancel(body, writer)
        else:
            writer.write(_json_response(HTTP_404, {"error": "not found"}))
            await writer.drain()

    # --- API handlers ---

    async def _handle_status(self, writer: asyncio.StreamWriter) -> None:
        agents = self._supervisor.agent_status()
        tasks = self._task_router.list_tasks()
        web_health = self._web_adapter.healthcheck()
        status = {
            "status": "running",
            "agents": agents,
            "tasks": {
                "total": len(tasks),
                "running": sum(1 for t in tasks if t["status"] == "running"),
                "pending": sum(1 for t in tasks if t["status"] == "pending"),
                "completed": sum(1 for t in tasks if t["status"] == "completed"),
            },
            "channels": {"web": web_health},
        }
        writer.write(_json_response(HTTP_200, status))
        await writer.drain()

    async def _handle_list_tasks(self, writer: asyncio.StreamWriter) -> None:
        tasks = self._task_router.list_tasks()
        writer.write(_json_response(HTTP_200, {"tasks": tasks}))
        await writer.drain()

    async def _handle_chat_send(
        self, body: bytes, writer: asyncio.StreamWriter
    ) -> None:
        try:
            data = json.loads(body.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            writer.write(_json_response(HTTP_400, {"error": "invalid JSON body"}))
            await writer.drain()
            return

        text = data.get("text", "").strip()
        if not text:
            writer.write(_json_response(HTTP_400, {"error": "text is required"}))
            await writer.drain()
            return

        result = await self._web_adapter.handle_user_message(
            text=text,
            session_id=data.get("session_id"),
            goal_id=data.get("goal_id"),
            language=data.get("language", "zh"),
            attachments=data.get("attachments"),
        )
        writer.write(_json_response(HTTP_200, result))
        await writer.drain()

    async def _handle_sse_events(
        self,
        query: dict[str, str],
        writer: asyncio.StreamWriter,
    ) -> None:
        cid = query.get("cid", "")
        if not cid:
            writer.write(_json_response(HTTP_400, {"error": "cid is required"}))
            await writer.drain()
            return

        sse_headers = (
            "HTTP/1.1 200 OK\r\n"
            "Content-Type: text/event-stream\r\n"
            "Cache-Control: no-cache\r\n"
            "Connection: keep-alive\r\n"
            "Access-Control-Allow-Origin: *\r\n"
            "\r\n"
        )
        writer.write(sse_headers.encode("utf-8"))
        await writer.drain()

        try:
            async for event in self._web_adapter.iter_sse_events(cid):
                kind = event.get("kind", "message")
                payload = json.dumps(event.get("payload", {}), ensure_ascii=False)
                sse_data = f"event: {kind}\ndata: {payload}\n\n"
                writer.write(sse_data.encode("utf-8"))
                await writer.drain()
        except (ConnectionError, BrokenPipeError):
            self._web_adapter.close_sse_stream(cid)

    async def _handle_task_cancel(
        self, body: bytes, writer: asyncio.StreamWriter
    ) -> None:
        try:
            data = json.loads(body.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            writer.write(_json_response(HTTP_400, {"error": "invalid JSON body"}))
            await writer.drain()
            return

        task_id = data.get("task_id", "").strip()
        if not task_id:
            writer.write(
                _json_response(HTTP_400, {"error": "task_id is required"})
            )
            await writer.drain()
            return

        from .bus import MessageBus
        from .message import TASK_ROUTER_ADDRESS, MessageEnvelope, MessageType

        bus = self._supervisor.bus
        envelope = MessageEnvelope.create(
            source=self._web_adapter._address,
            target=TASK_ROUTER_ADDRESS,
            message_type=MessageType.TASK_CANCEL,
            payload={"task_id": task_id},
        )
        await bus.publish(envelope)
        writer.write(_json_response(HTTP_200, {"cancelled": task_id}))
        await writer.drain()

    # --- Static file serving ---

    async def _serve_static(
        self, path: str, writer: asyncio.StreamWriter
    ) -> None:
        if path == "/":
            path = "/index.html"

        file_path = self._static_dir / path.lstrip("/")

        if not file_path.is_file():
            index_path = self._static_dir / "index.html"
            if index_path.is_file():
                file_path = index_path
            else:
                writer.write(_json_response(HTTP_404, {"error": "not found"}))
                await writer.drain()
                return

        try:
            resolved = file_path.resolve()
            static_resolved = self._static_dir.resolve()
            if not str(resolved).startswith(str(static_resolved)):
                writer.write(_json_response(HTTP_404, {"error": "not found"}))
                await writer.drain()
                return
        except (OSError, ValueError):
            writer.write(_json_response(HTTP_404, {"error": "not found"}))
            await writer.drain()
            return

        content_type, _ = mimetypes.guess_type(str(file_path))
        if content_type is None:
            content_type = "application/octet-stream"

        data = file_path.read_bytes()
        headers = (
            f"HTTP/1.1 {HTTP_200}\r\n"
            f"Content-Type: {content_type}\r\n"
            f"Content-Length: {len(data)}\r\n"
            f"Cache-Control: public, max-age=3600\r\n"
            f"Access-Control-Allow-Origin: *\r\n"
            f"\r\n"
        )
        writer.write(headers.encode("utf-8") + data)
        await writer.drain()
