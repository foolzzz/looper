"""Async HTTP server for the Looper agent system.

Pure stdlib asyncio — zero external dependencies. Serves:
  - REST API endpoints (/api/*)
  - SSE streaming (/api/chat/events: a message's stream by ?cid=, an open chat's by ?session=)
  - Static files from web/dist/ with SPA fallback
"""

from __future__ import annotations

import asyncio
import html
import json
import logging
import mimetypes
import os
import re
import secrets
import time
import uuid
import urllib.parse
from collections.abc import Awaitable, Callable, Mapping
from pathlib import Path
from typing import Any

from ..db import api_clients as db_api_clients
from ..db.tasks import TaskGroup
from ..ops_config import OpsConfig
from . import (
    agent_settings, api_v1, chat_settings, config_imports, config_transfer, gate_service, knowledge,
    operator_auth, overview, profile_registry, projects, prompt_preview, request_guard, role_api,
    schedules, self_check, vault, wait_service,
)
from .broadcast import Broadcaster
from .chat_targets import ChatTargetError, resolve_chat_target
from .message import AgentAddress
from .channels.web import WebChannelAdapter
from .scheduler import Scheduler
from .supervisor import AgentSupervisor
from .task_roles import record_role
from .ops import Call, ErrorCode, OpError, Risk, Tool, check_arguments
from .ops.clients import DEFAULT_SESSION, ClientOps
from .ops.planner import PlannerOps
from .ops.room import RoomOps
from .managers import ManagerFleet
from .ops.service import ManagerOps
from .tiers.manager import ManagerAgent, is_new_session_command
from .tiers.task import TaskRouter
from .tiers.worker import recheck_limits
from .tiers.worker import WorkerPool

logger = logging.getLogger(__name__)

# A source checkout serves the dashboard it builds (web/dist); an installed
# wheel serves the copy staged into the package (looper/web/dashboard).
_CHECKOUT_DASHBOARD = Path(__file__).resolve().parents[2] / "web" / "dist"
_PACKAGED_DASHBOARD = Path(__file__).resolve().parents[1] / "web" / "dashboard"


def default_static_dir() -> Path:
    if (_CHECKOUT_DASHBOARD / "index.html").is_file():
        return _CHECKOUT_DASHBOARD
    return _PACKAGED_DASHBOARD

def _serialize(obj: Any) -> Any:
    """Recursively convert datetime and other non-JSON types for response."""
    if isinstance(obj, dict):
        return {k: _serialize(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_serialize(v) for v in obj]
    if hasattr(obj, "isoformat"):
        return obj.isoformat()
    return obj


HTTP_200 = "200 OK"
HTTP_201 = "201 Created"
HTTP_303 = "303 See Other"
HTTP_400 = "400 Bad Request"
HTTP_401 = "401 Unauthorized"
HTTP_403 = "403 Forbidden"
HTTP_404 = "404 Not Found"
HTTP_405 = "405 Method Not Allowed"
HTTP_409 = "409 Conflict"
HTTP_413 = "413 Content Too Large"
HTTP_500 = "500 Internal Server Error"
HTTP_429 = "429 Too Many Requests"
HTTP_503 = "503 Service Unavailable"
# same-origin, not no-referrer: under no-referrer a browser sends `Origin: null` with the form's POST,
# which the same-origin check refuses. Nothing leaves for another site either way.
SIGN_IN_HEADERS = {
    "Cache-Control": "no-store", "Referrer-Policy": "same-origin",
    "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; frame-ancestors 'none'",
}

V1_HEADERS = {"Cache-Control": "no-store"}

# The Manager's tools (``ops.service``).
AGENT_API = "/api/agent/v1/"
_RE_AGENT_OP = re.compile(r"^/api/agent/v1/ops/([a-z_]{1,64})$")


# The largest request body the server reads. The largest legitimate one is a
# knowledge upload: a document of at most knowledge.DOCUMENT_MAX_BYTES (2 MiB)
# as base64 in JSON is about 2.7 MiB, and as JSON-escaped text up to twice its
# size; 8 MiB leaves room for both. A larger body is refused unread. The one
# exception is a configuration import (CONFIG_IMPORTS_PATH), which streams its
# body to a file and takes up to config_imports.UPLOAD_MAX_BYTES.
REQUEST_BODY_MAX_BYTES = 4 * knowledge.DOCUMENT_MAX_BYTES
# The Tasks list's page: how many tasks GET /api/tasks returns by default, and at most.
TASKS_PAGE = 50
TASKS_PAGE_MAX = 200
CONFIG_IMPORTS_PATH = "/api/config/imports"


def _json_response(status: str, body: Any, *, headers: Mapping[str, str] | None = None) -> bytes:
    payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
    head = (
        f"HTTP/1.1 {status}\r\n"
        f"Content-Type: application/json; charset=utf-8\r\n"
        f"Content-Length: {len(payload)}\r\n"
        + "".join(f"{key}: {value}\r\n" for key, value in (headers or {}).items())
        + "\r\n"
    )
    return head.encode("utf-8") + payload


def _no_content() -> bytes:
    # Same-origin requests never preflight, so an allowed OPTIONS gets no CORS headers.
    return b"HTTP/1.1 204 No Content\r\nContent-Length: 0\r\n\r\n"


def _with_run_role(record: dict[str, Any]) -> dict[str, Any]:
    """An ``llm_records`` row with ``task_role`` (``{id, kind, version}``) when its run ran as a role its task's
    Planner defined; a built-in role's run is as it was."""
    role = record_role(record)
    return {**record, "task_role": role} if role else record


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
        worker_pool: WorkerPool | None = None,
        manager: ManagerAgent | None = None,
        broadcaster: Broadcaster | None = None,
        static_dir: Path | None = None,
        runtime_root: Path | None = None,
        operator_token: str | None = None,
        dashboard_url: str | None = None,
        self_checks: list[dict[str, str]] | None = None,
        claude_bin: str = "claude",
        scheduler: Scheduler | None = None,
        manager_ops: ManagerOps | None = None,
        managers: ManagerFleet | None = None,
        planner_ops: PlannerOps | None = None,
        room_ops: RoomOps | None = None,
        client_ops: ClientOps | None = None,
        ops_config: OpsConfig | None = None,
    ) -> None:
        self._host = host
        self._port = port
        self._web_adapter = web_adapter
        self._task_router = task_router
        # Told to start the runs a raised concurrency limit lets start.
        self._worker_pool = worker_pool
        # Whether it is in a turn, for the overview.
        self._manager = manager
        self._supervisor = supervisor
        self._broadcaster = broadcaster or Broadcaster()
        self._static_dir = static_dir or default_static_dir()
        # Where the agents' files live: project knowledge is materialized under it.
        self._runtime_root = runtime_root
        # The CLI token and the local browser's credential; remote browsers use password sessions.
        self._operator_token = operator_token
        self._dashboard_url = dashboard_url
        self._sessions = operator_auth.Sessions()
        self._login_limiter = operator_auth.LoginLimiter()
        self._sign_in_lock = asyncio.Lock()
        self._password_key: tuple[int, ...] | None = None
        self._password: operator_auth.PasswordRecord | None = None
        self._password_error: operator_auth.OperatorTokenError | None = None
        # The startup self-check (self_check.Check.to_dict), reported on /api/v1/system/status.
        self._self_checks = self_checks or []
        self._ops_config = ops_config
        # The Claude Code the server runs: what a Claude Code runtime without an executable of its own probes.
        self._claude_bin = claude_bin
        # What "Run now" on a schedule fires through.
        self._scheduler = scheduler
        # The Manager's platform tools, behind /api/agent/v1/ (its token only), and each task's Planner's (its own).
        self._manager_ops = manager_ops
        # What an API client's operations run through (``/api/v1``).
        self._client_ops = client_ops
        self._managers = managers
        self._planner_ops = planner_ops
        # The agent room's tools of the steps' runs, each with a token of its own (``run.``).
        self._room_ops = room_ops
        # Configuration files uploaded for import, waiting to be applied (under the runtime root).
        self._config_imports = config_imports.ConfigImports(runtime_root) if runtime_root else None
        self._server: asyncio.Server | None = None

    async def start(self) -> None:
        if self._config_imports:
            self._config_imports.clear()  # what a previous server left pending
        self._server = await asyncio.start_server(
            self._handle_connection, self._host, self._port
        )
        if self._port == 0:
            self._port = self._server.sockets[0].getsockname()[1]
        logger.info("HTTP server listening on %s:%d", self._host, self._port)

    async def stop(self) -> None:
        if self._server:
            self._server.close()
            await self._server.wait_closed()
            logger.info("HTTP server stopped")
        if self._config_imports:
            self._config_imports.clear()

    async def _handle_connection(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        peer = writer.get_extra_info("peername")
        peer_host = peer[0] if peer else None
        sign_in_headers = None
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
            parsed = urllib.parse.urlparse(raw_path)
            path = parsed.path

            area = operator_auth.access(path)
            sign_in_headers = SIGN_IN_HEADERS if area is operator_auth.Access.SIGN_IN else None

            # Refused before its body is read: another site's page cannot even upload.
            refused = request_guard.refusal(headers, bind_host=self._host)
            if refused:
                logger.warning("refused %s %s: %s", method, path, refused)
                writer.write(_json_response(HTTP_403, {"error": refused}, headers=sign_in_headers))
                await writer.drain()
                writer.close()
                return

            if area is operator_auth.Access.OPERATOR and not self._is_operator(headers):
                credential = operator_auth.presented(headers, port=self._port)
                reason = operator_auth.WRONG if credential else operator_auth.REFUSAL
                logger.warning("refused %s %s from %s: no operator credential%s", method, path, peer_host,
                               " matched (one was presented)" if credential is not None else " was presented")
                writer.write(_json_response(HTTP_401, {"error": reason},
                                            headers={"WWW-Authenticate": 'Bearer realm="looper"'}))
                await writer.drain()
                writer.close()
                return

            principal = None
            if area is operator_auth.Access.CLIENT:
                try:
                    principal = await self._client_principal(headers, peer_host)
                except Exception:
                    logger.warning("could not check an API client token for %s %s from %s", method, path, peer_host,
                                   exc_info=True)
                    status, answer = api_v1.error(
                        ErrorCode.UNAVAILABLE, "Looper could not check the token now; try again")
                    writer.write(_json_response(status, answer, headers=V1_HEADERS))
                    await writer.drain()
                    writer.close()
                    return
                if principal is None:
                    logger.warning("refused %s %s from %s: no valid API client token%s", method, path, peer_host,
                                   " (one was presented)" if operator_auth.bearer(headers) is not None
                                   else " (none was presented)")
                    status, answer = api_v1.error(
                        ErrorCode.UNAUTHORIZED,
                        "a valid API client token is required: create one in Settings → API clients, "
                        "then run `loopercli login`")
                    writer.write(_json_response(status, answer, headers={
                        **V1_HEADERS, "WWW-Authenticate": 'Bearer realm="looper"'}))
                    await writer.drain()
                    writer.close()
                    return

            query = dict(urllib.parse.parse_qsl(
                parsed.query, keep_blank_values=area is operator_auth.Access.DASHBOARD))
            streamed = method == "POST" and path == CONFIG_IMPORTS_PATH
            cap = (config_imports.UPLOAD_MAX_BYTES if streamed or path == "/api/v1/config/import_preview"
                   else REQUEST_BODY_MAX_BYTES)
            if area is operator_auth.Access.SIGN_IN:
                cap = 4096
            body = b""
            try:
                content_length = int(headers.get("content-length", "0"))
            except ValueError:
                content_length = -1
            if content_length < 0:
                writer.write(_json_response(HTTP_400, {"error": "bad Content-Length"}, headers=sign_in_headers))
                await writer.drain()
                return
            if content_length > cap:
                writer.write(_json_response(HTTP_413, {
                    "error": f"the request body has {content_length} bytes; "
                             f"{'a configuration import' if streamed else 'the server'} takes at most "
                             f"{cap // (1024 * 1024)} MiB ({cap} bytes)",
                }, headers=sign_in_headers))
                await writer.drain()
                return
            if streamed:
                if headers.get("expect", "").lower() == "100-continue":
                    writer.write(b"HTTP/1.1 100 Continue\r\n\r\n")
                    await writer.drain()
                await self._handle_config_import(
                    writer, HTTP_201, lambda imports: imports.receive(reader, content_length))
                return
            if content_length > 0:
                body = await asyncio.wait_for(
                    reader.readexactly(content_length), timeout=30.0
                )

            if method == "OPTIONS" and area is operator_auth.Access.DASHBOARD:
                writer.write(_no_content())
                await writer.drain()
                writer.close()
                return

            if area is operator_auth.Access.SIGN_IN:
                await self._handle_sign_in(method, path, headers, body, peer_host, writer)
            elif area is operator_auth.Access.AGENT:
                await self._handle_agent_api(method, path, headers, body, writer)
            elif area is operator_auth.Access.CLIENT:
                if method == "POST" and path == "/api/v1/tasks/activity_stream":
                    stream = asyncio.create_task(self._handle_v1(method, path, query, headers, body, writer, principal))
                    disconnected = asyncio.create_task(reader.read())
                    try:
                        await asyncio.wait((stream, disconnected), return_when=asyncio.FIRST_COMPLETED)
                    finally:
                        for task in (stream, disconnected):
                            if not task.done():
                                task.cancel()
                        await asyncio.gather(stream, disconnected, return_exceptions=True)
                else:
                    await self._handle_v1(method, path, query, headers, body, writer, principal)
            elif area is operator_auth.Access.OPERATOR:
                await self._route_api(method, path, query, body, writer, headers)
            else:
                await self._serve_static(path, writer, headers=headers, query=query, peer_host=peer_host)

        except (asyncio.TimeoutError, ConnectionError, asyncio.IncompleteReadError):
            pass
        except Exception:
            logger.exception("error handling HTTP request")
            try:
                writer.write(_json_response(HTTP_500, {"error": "internal server error"}, headers=sign_in_headers))
                await writer.drain()
            except Exception:
                pass
        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass

    async def _client_principal(self, headers: Mapping[str, str], peer_host: str | None) -> api_v1.Principal | None:
        """Who calls ``/api/v1``: the operator, an API client (a local, direct caller with an ``lpc_`` bearer token),
        or nobody. ``find`` raises when the database is down."""
        if self._is_operator(headers):
            return api_v1.OPERATOR
        token = operator_auth.bearer(headers)
        if (token is None or not token.startswith(db_api_clients.TOKEN_PREFIX)
                or peer_host is None or not operator_auth.is_loopback(peer_host)
                or not operator_auth.is_loopback(operator_auth.hostname(headers.get("host", "")))
                or "forwarded" in headers or "x-forwarded-for" in headers):
            return None
        client = await db_api_clients.find(token)
        if client is None:
            return None
        try:
            await db_api_clients.touch(client["id"])
        except Exception:
            logger.debug("could not note the use of API client %s", client["id"], exc_info=True)
        return api_v1.Principal("client", client["id"], client["name"])

    async def _handle_v1(self, method: str, path: str, query: dict[str, str], headers: Mapping[str, str],
                         body: bytes, writer: asyncio.StreamWriter, principal: api_v1.Principal) -> None:
        started = time.monotonic()
        operation = api_v1.find(method, path)
        refused: tuple[str, dict[str, Any]] | None = None
        if operation is not None and operation.operator_only and principal.kind != "operator":
            status, answer = api_v1.error(ErrorCode.FORBIDDEN, "operator identity is required")
        elif operation is not None and operation.stream and api_v1.domain_for(operation) is not None:
            refused = await self._v1_domain(operation, headers, body, principal, writer=writer)
            status, answer = refused or (api_v1.OK, {"status": "ok"})
        elif operation is not None and operation.operation_id == "chat.send":
            refused = await self._v1_chat_send(body, principal, writer)
            status, answer = refused or (api_v1.OK, {"status": "ok"})
        else:
            status, answer = await self._answer_v1(method, path, operation, headers, body, principal)
        if operation is not None and operation.operation_id != "openapi":
            outcome = (answer["status"] if answer.get("status") in ("ok", "needs_confirmation")
                       else f"error:{answer['error']['code']}")
            logger.info("api client=%s op=%s status=%s ms=%d", principal.key, operation.operation_id, outcome,
                        (time.monotonic() - started) * 1000)
        if operation is not None and operation.stream and refused is None and answer.get("status") == "ok":
            return  # the stream is written
        writer.write(_json_response(status, answer, headers=V1_HEADERS))
        await writer.drain()

    @staticmethod
    def _v1_body(body: bytes) -> dict[str, Any] | None:
        """The JSON object a request carries (empty for no body); None when it is anything else."""
        try:
            data = json.loads(body) if body.strip() else {}
        except ValueError:
            return None
        return data if isinstance(data, dict) else None

    async def _answer_v1(self, method: str, path: str, operation: api_v1.Operation | None,
                         headers: Mapping[str, str], body: bytes,
                         principal: api_v1.Principal) -> tuple[str, dict[str, Any]]:
        if operation is None:
            other = api_v1.allowed_method(path)
            if other is not None:
                return api_v1.error(ErrorCode.INVALID, f"use {other}", status=HTTP_405)
            return api_v1.error(ErrorCode.NOT_FOUND, f"no {method} {path}")
        if operation.operator_only and principal.kind != "operator":
            return api_v1.error(ErrorCode.FORBIDDEN, "operator identity is required")
        if api_v1.domain_for(operation) is not None:
            return await self._v1_domain(operation, headers, body, principal)
        if operation.operation_id == "openapi":
            return api_v1.OK, api_v1.document()
        if operation.operation_id == "whoami":
            return api_v1.OK, api_v1.whoami(principal)
        if operation.operation_id == "chat.messages":
            return await self._v1_chat_messages(body, principal, operation)
        if operation.operation_id == "chat.conversations":
            return await self._v1_chat_conversations(body, principal, operation)
        if operation in api_v1.ONLINE_OPERATIONS:
            return await self._v1_online(operation, headers, body, principal)
        if self._client_ops is None:
            return api_v1.error(ErrorCode.UNAVAILABLE, "the API is not ready")
        session = headers.get("x-looper-session") or DEFAULT_SESSION
        data = self._v1_body(body)
        if data is None:
            return api_v1.error(ErrorCode.INVALID, "the body must be a JSON object")
        if operation.tool is not None:
            if "action" in data and operation.action:
                return api_v1.error(ErrorCode.INVALID, "action is part of the path")
            arguments = {**data, "action": operation.action} if operation.action else data
            answer = await self._call_v1(principal, session, operation.tool, arguments)
        elif operation.operation_id == "confirm":
            confirmation_id = data.get("confirmation_id")
            if not isinstance(confirmation_id, str) or not confirmation_id:
                return api_v1.error(ErrorCode.INVALID, "confirmation_id is required")
            answer = await self._client_ops.confirm(principal, session, confirmation_id)
        else:
            return api_v1.error(ErrorCode.NOT_FOUND, "not available yet")
        return api_v1.answer_status(answer), answer

    async def _call_v1(self, principal: api_v1.Principal, session: str, name: str, data: Any,
                       **kwargs: Any) -> dict[str, Any]:
        return await self._client_ops.call(principal, session, name, data,
                                          require_confirmation=principal.kind == "client", **kwargs)

    async def _v1_domain(self, operation: api_v1.Operation, headers: Mapping[str, str], body: bytes,
                         principal: api_v1.Principal, *, writer: asyncio.StreamWriter | None = None
                         ) -> tuple[str, dict[str, Any]] | None:
        domain = api_v1.domain_for(operation)
        if operation.operator_only and principal.kind != "operator":
            return api_v1.error(ErrorCode.FORBIDDEN, "operator identity is required")

        async def invoke(callback: Any, arguments: dict[str, Any], *extra: Any) -> Any:
            try:
                return await callback(self, operation, arguments, principal, headers, *extra)
            except OpError:
                if not operation.credential_write:
                    raise
                raise OpError(ErrorCode.INVALID, "invalid credential request") from None
            except projects.RequestError as exc:
                code = next((code for code, status in api_v1.STATUS.items() if status == exc.status), ErrorCode.INVALID)
                raise OpError(code, "the operation was refused") from None
            except ValueError:
                raise OpError(ErrorCode.INVALID, "invalid request") from None
            except Exception:
                logger.warning("domain operation %s failed", operation.operation_id)
                raise OpError(ErrorCode.UNAVAILABLE, "the operation failed; try again later") from None

        try:
            data = self._v1_body(body)
            if data is None:
                raise OpError(ErrorCode.INVALID, "the body must be a JSON object")
            # Only explicitly declared dashboard operations bypass the operator action trail.
            if ((principal.kind == "operator" and not operation.audit_operator)
                    or writer is not None or operation.credential_write
                    or operation.risk in (None, "read")):
                arguments = api_v1.validate_arguments(operation.request or {}, data)
                if writer is not None:
                    return await invoke(domain.stream, arguments, writer)
                return api_v1.OK, {"status": "ok", "result": _serialize(await invoke(domain.run, arguments))}
            if self._client_ops is None:
                raise OpError(ErrorCode.UNAVAILABLE, "the API is not ready")

            async def run(call: Call) -> dict[str, Any]:
                return await invoke(domain.run, call.arguments)

            async def impact(call: Call) -> str | None:
                result = await invoke(domain.impact, call.arguments)
                if operation.risk == "high" and not isinstance(result, str):
                    raise OpError(ErrorCode.UNAVAILABLE, "the operation has no confirmation impact")
                return result

            tool = Tool(name=operation.operation_id, description=operation.description, schema=operation.request or {},
                        risk=Risk(operation.risk), run=run,
                        escalate=impact if callable(getattr(domain, "impact", None)) else None)
            answer = await self._call_v1(principal, headers.get("x-looper-session") or DEFAULT_SESSION,
                                         tool.name, data, tool=tool,
                                         validator=getattr(domain, "validate_arguments", api_v1.validate_arguments))
            return api_v1.answer_status(answer), answer
        except OpError as exc:
            return api_v1.error(exc.code, "invalid credential request" if operation.credential_write else str(exc),
                                details=None if operation.credential_write else exc.details)

    async def _v1_online(self, operation: api_v1.Operation, headers: Mapping[str, str], body: bytes,
                         principal: api_v1.Principal) -> tuple[str, dict[str, Any]]:
        name = operation.operation_id
        # Refuse before validating arguments: credential requests never enter the ops argument audit trail.
        if operation.operator_only and principal.kind != "operator":
            return api_v1.error(ErrorCode.FORBIDDEN, "operator identity is required")
        data = self._v1_body(body)
        if data is None:
            return api_v1.error(ErrorCode.INVALID, "the body must be a JSON object")
        try:
            # Validation errors can contain values; never echo a password supplied as an unexpected property.
            try:
                schema = operation.request or {}
                if name == "auth.set_password":
                    # The tool validator trims minLength; passwords must keep the offline command's raw length.
                    schema = {**schema, "properties": {**schema["properties"], "password": {"type": "string"}}}
                data = check_arguments(schema, data)
            except OpError:
                if operation.operator_only:
                    raise OpError(ErrorCode.INVALID, "invalid credential request") from None
                raise
            if operation.operator_only:
                if self._runtime_root is None:
                    raise OpError(ErrorCode.UNAVAILABLE, "the server has no runtime root")
                async with self._sign_in_lock:
                    if name == "auth.set_password":
                        user = data.get("user", "admin")
                        if not isinstance(user, str) or not isinstance(data["password"], str):
                            raise OpError(ErrorCode.INVALID, "invalid credential request")
                        await asyncio.to_thread(operator_auth.set_password, self._runtime_root, user, data["password"])
                        result = {"user": user, "password_set": True}
                    else:
                        result = {"removed": await asyncio.to_thread(operator_auth.clear_password, self._runtime_root)}
                    self._sessions = operator_auth.Sessions()
                    self._password_key = None
                return api_v1.OK, {"status": "ok", "result": result}
            if name == "system.check":
                if self._ops_config is None:
                    raise OpError(ErrorCode.UNAVAILABLE, "the server has no ops settings for self checks")
                checks = await self_check.run_checks(self._ops_config, claude_bin=self._claude_bin)
                return api_v1.OK, {"status": "ok", "result": {"checks": [c.to_dict() for c in checks]}}
            if self._client_ops is None:
                raise OpError(ErrorCode.UNAVAILABLE, "the API is not ready")

            async def run(call: Call) -> dict[str, Any]:
                try:
                    if name == "config.export":
                        from ..db.connection import get_pool
                        async with get_pool().acquire() as conn, conn.transaction(isolation="repeatable_read", readonly=True):
                            chunks = config_transfer.export_chunks(conn,
                                include_projects=bool(call.arguments.get("include_projects")),
                                include_secrets=bool(call.arguments.get("include_secrets")))
                            document = json.loads("".join([chunk async for chunk in chunks]))
                        return {"document": document}
                    if self._config_imports is None:
                        raise OpError(ErrorCode.UNAVAILABLE, "the server has no runtime root")
                    if name == "config.import_preview":
                        raw = json.dumps(call.arguments["document"], ensure_ascii=False).encode()
                        reader = asyncio.StreamReader()
                        reader.feed_data(raw)
                        reader.feed_eof()
                        return await self._config_imports.receive(reader, len(raw), owner=principal.key)
                    if name == "config.import_cancel":
                        return await self._config_imports.cancel(call.arguments["import_id"], owner=principal.key)
                    return await self._apply_import(self._config_imports, call.arguments["import_id"],
                                                    owner=principal.key)
                except config_transfer.ConfigFileError as exc:
                    raise OpError(ErrorCode.INVALID, str(exc)) from None
                except config_imports.ImportNotFound as exc:
                    raise OpError(ErrorCode.NOT_FOUND, str(exc)) from None
                except (config_imports.ImportConflict, config_transfer.SchemaChangePending) as exc:
                    raise OpError(ErrorCode.CONFLICT, str(exc)) from None

            async def impact(call: Call) -> str:
                if self._config_imports is None:
                    raise OpError(ErrorCode.UNAVAILABLE, "the server has no runtime root")
                try:
                    self._config_imports._get(call.arguments["import_id"], owner=principal.key)
                except config_imports.ImportNotFound as exc:
                    raise OpError(ErrorCode.NOT_FOUND, str(exc)) from None
                return f"Apply configuration import {call.arguments['import_id']} in one transaction."

            applying = name == "config.import_apply"
            tool = Tool(name=name, description=operation.description, schema=operation.request or {},
                        risk=Risk.HIGH if applying else Risk.READ, run=run, escalate=impact if applying else None)
            answer = await self._call_v1(principal, headers.get("x-looper-session") or DEFAULT_SESSION,
                                                  name, data, tool=tool)
            return api_v1.answer_status(answer), answer
        except OpError as exc:
            return api_v1.error(exc.code, str(exc))
        except ValueError:
            return api_v1.error(ErrorCode.INVALID, "invalid credential request" if operation.operator_only
                                else "invalid request")
        except Exception:
            # No exception text or traceback: credential failures must not expose request values.
            logger.warning("online operation %s failed", name)
            return api_v1.error(ErrorCode.UNAVAILABLE, "the operation failed; try again later")

    # --- /api/v1 chat ---

    @classmethod
    def _v1_chat_arguments(cls, body: bytes, principal: api_v1.Principal,
                           operation: api_v1.Operation) -> dict[str, Any] | tuple[str, dict[str, Any]]:
        """The checked arguments of a chat call, or its refusal: the operator has the dashboard's chat, and the
        arguments are those the operation declares (a client cannot name itself or pass a key)."""
        if principal.kind != "client":
            return api_v1.error(
                ErrorCode.FORBIDDEN, "loopercli chat needs an API client token; the dashboard has its own chat")
        data = cls._v1_body(body)
        if data is None:
            return api_v1.error(ErrorCode.INVALID, "the body must be a JSON object")
        try:
            return check_arguments(operation.request or {}, data)
        except OpError as exc:
            return api_v1.error(exc.code, str(exc))

    async def _v1_chat_send(self, body: bytes, principal: api_v1.Principal,
                            writer: asyncio.StreamWriter) -> tuple[str, dict[str, Any]] | None:
        """``POST /api/v1/chat/send``: the message goes to the Manager and its reply streams back as server-sent
        events; None once the stream is written, else the refusal."""
        from ..db import chat as db_chat

        operation = api_v1.find("POST", f"{api_v1.PREFIX}/chat/send")
        assert operation is not None
        arguments = self._v1_chat_arguments(body, principal, operation)
        if isinstance(arguments, tuple):
            return arguments
        text = arguments["text"].strip()
        if not text:
            return api_v1.error(ErrorCode.INVALID, "text is required")
        manager_id, conversation_id = arguments.get("manager_id"), arguments.get("conversation_id")
        try:
            target = await resolve_chat_target(
                {"kind": "manager", "id": manager_id} if manager_id else None, self._task_router, self._managers)
            if conversation_id is None:
                conversation_id = api_v1.CONVERSATION_PREFIX + secrets.token_hex(8)
                await db_chat.claim_thread(conversation_id, target.agent_id, client_id=principal.client_id)
            else:
                if await db_chat.thread_client(conversation_id) != principal.client_id:
                    return api_v1.error(ErrorCode.NOT_FOUND, f"no conversation {conversation_id} of this client")
                owner = await db_chat.thread_owner(conversation_id) or "manager"
                if manager_id and manager_id != owner:
                    return api_v1.error(
                        ErrorCode.CONFLICT, f"conversation {conversation_id} belongs to Manager {owner}")
                target = await resolve_chat_target(
                    None if owner == "manager" and self._managers is None else {"kind": "manager", "id": owner},
                    self._task_router, self._managers)
        except ChatTargetError as exc:
            return api_v1.error(ErrorCode.CONFLICT if exc.status == HTTP_409 else ErrorCode.INVALID, str(exc))
        message_id = f"cli-m-{uuid.uuid4().hex[:12]}"
        result = await self._deliver_chat(
            text=text, target=target, session_id=conversation_id, message_id=message_id,
            language=arguments.get("language") or "zh",
            client={"id": principal.client_id, "name": principal.client_name})
        correlation_id = result["correlation_id"]

        def frame(kind: str, payload: Mapping[str, Any]) -> bytes:
            return f"event: {kind}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n".encode("utf-8")

        try:
            writer.write((
                "HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nCache-Control: no-store\r\n"
                "X-Accel-Buffering: no\r\nConnection: close\r\n\r\n").encode("utf-8"))
            writer.write(frame("accepted", {
                "conversation_id": conversation_id, "message_id": message_id, "manager_id": target.agent_id}))
            await writer.drain()
            finished = False
            async for event in self._web_adapter.iter_sse_events(correlation_id, timeout=api_v1.CHAT_STREAM_SECONDS):
                kind, payload = event.get("kind", "message"), event.get("payload") or {}
                if kind == "answer.delta":
                    writer.write(frame("delta", {"text": payload.get("text", "")}))
                elif kind == "agent.phase":
                    writer.write(frame("status", {"label": payload.get("label", "")}))
                elif kind == "answer.final":
                    finished = True
                    writer.write(frame("final", {"text": payload.get("text", ""),
                                                 "outcome": payload.get("outcome", "answered")}))
                elif kind == "heartbeat":
                    writer.write(frame("heartbeat", {}))
                else:
                    writer.write(frame(kind, payload))
                await writer.drain()
            if not finished:
                writer.write(frame("timeout", {
                    "message": "the reply is still being written; read it later with: "
                               f"loopercli chat --conversation {conversation_id} --read"}))
                await writer.drain()
        except (ConnectionError, BrokenPipeError):
            # The reply is still saved to the thread by the adapter.
            self._web_adapter.close_sse_stream(correlation_id)
        return None

    async def _v1_chat_messages(self, body: bytes, principal: api_v1.Principal,
                                operation: api_v1.Operation) -> tuple[str, dict[str, Any]]:
        """``POST /api/v1/chat/messages``: a page of one of the client's conversations after a cursor."""
        from ..db import chat as db_chat

        arguments = self._v1_chat_arguments(body, principal, operation)
        if isinstance(arguments, tuple):
            return arguments
        conversation_id, after = arguments["conversation_id"], arguments.get("after")
        if await db_chat.thread_client(conversation_id) != principal.client_id:
            return api_v1.error(ErrorCode.NOT_FOUND, f"no conversation {conversation_id} of this client")
        try:
            page, has_more = await db_chat.messages_page(
                conversation_id, after=after, limit=arguments.get("limit") or 100)
        except db_chat.UnknownCursor:
            return api_v1.error(ErrorCode.INVALID, f"unknown cursor {after} in conversation {conversation_id}; "
                                                   "read from the start without --after")
        messages = [{"message_id": m["message_id"], "role": m["role"], "content": m["content"],
                     "created_at": m["created_at"]} for m in page]
        return api_v1.OK, {"status": "ok", "result": _serialize({
            "conversation_id": conversation_id,
            "manager_id": await db_chat.thread_owner(conversation_id) or "manager",
            "messages": messages,
            "next_cursor": messages[-1]["message_id"] if messages else after,
            "has_more": has_more})}

    async def _v1_chat_conversations(self, body: bytes, principal: api_v1.Principal,
                                     operation: api_v1.Operation) -> tuple[str, dict[str, Any]]:
        """``POST /api/v1/chat/conversations``: the client's own conversations."""
        from ..db import chat as db_chat

        arguments = self._v1_chat_arguments(body, principal, operation)
        if isinstance(arguments, tuple):
            return arguments
        threads = await db_chat.client_threads(principal.client_id)
        return api_v1.OK, {"status": "ok", "result": _serialize({"conversations": [
            {"conversation_id": t["session_id"], "manager_id": t["manager_id"], "created_at": t["created_at"],
             "last_message_at": t["last_message_at"], "last_content": t["last_content"]} for t in threads]})}

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

    _RE_TASK_DETAIL = re.compile(r"^/api/tasks/(task-[a-zA-Z0-9]+)$")
    _RE_CHANNEL_CONFIG = re.compile(r"^/api/channel-configs/([a-zA-Z0-9_-]+)$")
    _RE_TASK_WORKSPACE_DISCARD = re.compile(r"^/api/tasks/(task-[a-zA-Z0-9]+)/workspaces/(\d{1,18})/discard$")
    _RE_PROJECT_KNOWLEDGE = re.compile(r"^/api/projects/(proj-[a-zA-Z0-9]+)/knowledge$")
    _RE_PROJECT_DOCUMENT = re.compile(r"^/api/projects/(proj-[a-zA-Z0-9]+)/knowledge/(doc-[a-zA-Z0-9]+)$")
    _RE_PROJECT_DRAFTS = re.compile(r"^/api/projects/(proj-[a-zA-Z0-9]+)/knowledge/drafts$")
    _RE_PROJECT_DRAFT = re.compile(r"^/api/projects/(proj-[a-zA-Z0-9]+)/knowledge/drafts/(draft-[a-zA-Z0-9]+)$")
    _RE_PROJECT_DRAFT_CONFIRM = re.compile(
        r"^/api/projects/(proj-[a-zA-Z0-9]+)/knowledge/drafts/(draft-[a-zA-Z0-9]+)/confirm$")
    _RE_PROJECT_AGENT_SETTINGS = re.compile(r"^/api/projects/(proj-[a-zA-Z0-9]+)/agent-settings$")
    _RE_PROJECT_ROLE_SETTINGS = re.compile(r"^/api/projects/(proj-[a-zA-Z0-9]+)/agent-settings/roles/([a-zA-Z0-9_-]+)$")
    _RE_PROJECT_ALLOWED_PROFILES = re.compile(r"^/api/projects/(proj-[a-zA-Z0-9]+)/allowed-profiles$")
    _RE_RUNTIME_PROBE = re.compile(r"^/api/runtimes/(rt-[a-zA-Z0-9-]+)/probe$")
    _RE_PROVIDER = re.compile(r"^/api/providers/(prov-[a-zA-Z0-9-]+)$")
    _RE_MODEL = re.compile(r"^/api/models/(model-[a-zA-Z0-9-]+)$")
    _RE_PROVIDER_LOGIN_PROBLEM = re.compile(r"^/api/providers/(prov-[a-zA-Z0-9-]+)/login-problem$")
    _RE_PROFILE_PROMPT_PREVIEW = re.compile(r"^/api/agent-profiles/(agent-[a-zA-Z0-9-]+)/prompt-preview$")
    _RE_PROFILE = re.compile(r"^/api/agent-profiles/(agent-[a-zA-Z0-9-]+)$")
    _RE_CONFIG_IMPORT = re.compile(r"^/api/config/imports/(cfgimp-[0-9a-f]{16})$")
    _RE_CONFIG_IMPORT_APPLY = re.compile(r"^/api/config/imports/(cfgimp-[0-9a-f]{16})/apply$")

    async def _route_api(
        self,
        method: str,
        path: str,
        query: dict[str, str],
        body: bytes,
        writer: asyncio.StreamWriter,
        headers: Mapping[str, str] | None = None,
    ) -> None:
        if path == "/api/tasks" and method == "GET":
            await self._handle_list_tasks(query, writer)
        elif path == "/api/chat/send" and method == "POST":
            await self._handle_chat_send(body, writer)
        elif path == "/api/chat/events" and method == "GET":
            await self._handle_sse_events(query, writer)
        elif (m := self._RE_TASK_DETAIL.match(path)) and method == "GET":
            await self._handle_task_detail(m.group(1), writer)
        elif (m := self._RE_PROJECT_KNOWLEDGE.match(path)) and method == "GET":
            await self._handle_knowledge(writer, HTTP_200, lambda root: knowledge.knowledge_view(m.group(1), root))
        elif (m := self._RE_PROJECT_KNOWLEDGE.match(path)) and method == "POST":
            await self._handle_knowledge(writer, HTTP_201, lambda root: knowledge.add_document(
                m.group(1), projects.json_object(body), root))
        elif (m := self._RE_PROJECT_DOCUMENT.match(path)) and method == "GET":
            await self._handle_knowledge(writer, HTTP_200, lambda root: knowledge.get_document(
                m.group(1), m.group(2), root))
        elif (m := self._RE_PROJECT_DOCUMENT.match(path)) and method == "PUT":
            await self._handle_knowledge(writer, HTTP_200, lambda root: knowledge.update_document(
                m.group(1), m.group(2), projects.json_object(body), root))
        elif (m := self._RE_PROJECT_DOCUMENT.match(path)) and method == "DELETE":
            await self._handle_knowledge(writer, HTTP_200, lambda root: knowledge.delete_document(
                m.group(1), m.group(2), root))
        elif (m := self._RE_PROJECT_DRAFTS.match(path)) and method == "GET":
            await self._handle_knowledge(writer, HTTP_200, lambda root: knowledge.drafts_view(m.group(1)))
        elif (m := self._RE_PROJECT_DRAFT_CONFIRM.match(path)) and method == "POST":
            # The body is optional: the title, summary or content to confirm the draft with.
            await self._handle_knowledge(writer, HTTP_200, lambda root: knowledge.confirm_draft(
                m.group(1), m.group(2), projects.json_object(body) if body.strip() else {}, root))
        elif (m := self._RE_PROJECT_DRAFT.match(path)) and method == "DELETE":
            await self._handle_knowledge(writer, HTTP_200, lambda root: knowledge.discard_draft(
                m.group(1), m.group(2)))
        elif path == "/api/registry" and method == "GET":
            await self._handle_registry(writer, HTTP_200, lambda: profile_registry.registry_view(
                server_claude=self._claude_bin))
        elif (m := self._RE_RUNTIME_PROBE.match(path)) and method == "POST":
            await self._handle_registry(writer, HTTP_200, lambda: profile_registry.probe_runtime(
                m.group(1), server_claude=self._claude_bin))
        elif path == "/api/providers" and method == "GET":
            await self._handle_registry(writer, HTTP_200, profile_registry.list_providers)
        elif path == "/api/providers" and method == "POST":
            await self._handle_registry(writer, HTTP_201, lambda: profile_registry.create_provider(
                projects.json_object(body)))
        elif (m := self._RE_PROVIDER.match(path)) and method == "PUT":
            await self._handle_registry(writer, HTTP_200, lambda: profile_registry.update_provider(
                m.group(1), projects.json_object(body)))
        elif (m := self._RE_PROVIDER.match(path)) and method == "DELETE":
            await self._handle_registry(writer, HTTP_200, lambda: profile_registry.delete_provider(m.group(1)))
        elif (m := self._RE_PROVIDER_LOGIN_PROBLEM.match(path)) and method == "DELETE":
            await self._handle_registry(writer, HTTP_200, lambda: profile_registry.clear_login_problem(m.group(1)))
        elif path == "/api/models" and method == "GET":
            await self._handle_registry(writer, HTTP_200, lambda: profile_registry.list_models(
                provider_id=query.get("provider_id") or None, runtime_id=query.get("runtime_id") or None))
        elif path == "/api/models" and method == "POST":
            await self._handle_registry(writer, HTTP_201, lambda: profile_registry.create_model(
                projects.json_object(body)))
        elif (m := self._RE_MODEL.match(path)) and method == "GET":
            await self._handle_registry(writer, HTTP_200, lambda: profile_registry.get_model(m.group(1)))
        elif (m := self._RE_MODEL.match(path)) and method == "PUT":
            await self._handle_registry(writer, HTTP_200, lambda: profile_registry.update_model(
                m.group(1), projects.json_object(body)))
        elif (m := self._RE_MODEL.match(path)) and method == "DELETE":
            await self._handle_registry(writer, HTTP_200, lambda: profile_registry.delete_model(m.group(1)))
        elif (m := self._RE_PROFILE_PROMPT_PREVIEW.match(path)) and method == "GET":
            await self._handle_registry(writer, HTTP_200, lambda: prompt_preview.preview(
                m.group(1), query.get("role", ""), query.get("project_id") or None))
        elif path == "/api/agent-profiles" and method == "POST":
            await self._handle_registry(writer, HTTP_201, lambda: profile_registry.create_profile(
                projects.json_object(body)))
        elif (m := self._RE_PROFILE.match(path)) and method == "PUT":
            await self._handle_registry(writer, HTTP_200, lambda: profile_registry.update_profile(
                m.group(1), projects.json_object(body)))
        elif (m := self._RE_PROFILE.match(path)) and method == "DELETE":
            await self._handle_registry(writer, HTTP_200, lambda: profile_registry.delete_profile(m.group(1)))
        elif (m := self._RE_PROJECT_AGENT_SETTINGS.match(path)) and method == "GET":
            await self._handle_agent_settings(writer, lambda: agent_settings.project_settings_view(m.group(1)))
        elif (m := self._RE_PROJECT_AGENT_SETTINGS.match(path)) and method == "PUT":
            await self._handle_agent_settings(writer, lambda: agent_settings.update_project(
                m.group(1), projects.json_object(body)))
        elif (m := self._RE_PROJECT_ALLOWED_PROFILES.match(path)) and method == "GET":
            await self._answer(writer, HTTP_200, lambda: projects.allowed_profiles_view(m.group(1)))
        elif (m := self._RE_PROJECT_ALLOWED_PROFILES.match(path)) and method == "PUT":
            await self._answer(writer, HTTP_200, lambda: projects.set_allowed_profiles(
                m.group(1), projects.json_object(body)))
        elif (m := self._RE_PROJECT_ROLE_SETTINGS.match(path)) and method == "PUT":
            await self._handle_agent_settings(writer, lambda: self._write_layer(
                body, lambda data: agent_settings.update_project(m.group(1), data, role=m.group(2))))
        elif path == "/api/config/export" and method == "GET":
            await self._handle_config_export(query, writer)
        elif (m := self._RE_CONFIG_IMPORT_APPLY.match(path)) and method == "POST":
            await self._handle_config_import(writer, HTTP_200,
                                             lambda imports: self._apply_import(imports, m.group(1), body))
        elif (m := self._RE_CONFIG_IMPORT.match(path)) and method == "DELETE":
            await self._handle_config_import(writer, HTTP_200, lambda imports: imports.cancel(m.group(1)))
        elif path == "/api/scheduler-settings" and method == "GET":
            await self._answer(writer, HTTP_200, schedules.scheduler_settings)
        elif path == "/api/scheduler-settings" and method == "PUT":
            await self._answer(writer, HTTP_200,
                               lambda: schedules.update_scheduler_settings(projects.json_object(body)))
        elif path == "/api/wait-settings" and method == "GET":
            await self._answer(writer, HTTP_200, wait_service.settings_view)
        elif path == "/api/wait-settings" and method == "PUT":
            await self._answer(writer, HTTP_200, lambda: wait_service.update_settings(projects.json_object(body)))
        elif path == "/api/knowledge-vault" and method == "GET":
            await self._answer(writer, HTTP_200, vault.settings_view)
        elif path == "/api/knowledge-vault" and method == "PUT":
            await self._answer(writer, HTTP_200, lambda: vault.update_settings(projects.json_object(body)))
        else:
            writer.write(_json_response(HTTP_404, {"error": "not found"}))
            await writer.drain()

    # --- API handlers ---

    async def _handle_agent_api(
        self, method: str, path: str, headers: dict[str, str], body: bytes, writer: asyncio.StreamWriter,
    ) -> None:
        """The Manager's tools (``ops.service``): ``GET tools`` lists them, ``POST ops/<tool>`` calls one with the
        JSON body as its arguments and answers the protocol's object, 200 also for a refused call. Only the
        token of this server's Manager gets in, or a Planner's token, for that Planner's own tools
        (``ops.planner``)."""
        if self._planner_ops is not None and self._planner_ops.claims(headers.get("authorization")):
            await self._handle_planner_api(method, path, headers, body, writer)
            return
        if self._room_ops is not None and self._room_ops.claims(headers.get("authorization")):
            await self._handle_room_api(method, path, headers, body, writer)
            return
        authorization = headers.get("authorization")
        ops = self._managers.ops_for(authorization) if self._managers is not None else self._manager_ops
        if ops is None or not ops.authorized(authorization):
            # With a fleet the token is nobody's (unknown, or a stopped Manager's): no Manager's turn is touched.
            if self._managers is None and self._manager_ops is not None:
                self._manager_ops.refuse(path)
            ops = None
            writer.write(_json_response(HTTP_401, {"error": "this endpoint is the Manager's: a valid token is "
                                                            "required"}))
        elif path == f"{AGENT_API}tools" and method == "GET":
            try:
                writer.write(_json_response(HTTP_200, {"tools": await ops.current_listing()}))
            except OpError as exc:
                writer.write(_json_response(HTTP_503, {"error": str(exc)}))
        elif (m := _RE_AGENT_OP.match(path)) and method == "POST":
            try:
                arguments = json.loads(body.decode("utf-8")) if body.strip() else {}
            except (json.JSONDecodeError, UnicodeDecodeError):
                arguments = None
            writer.write(_json_response(HTTP_200, await ops.call(
                m.group(1), arguments, binding=headers.get("x-looper-turn"), call_id=headers.get("x-looper-call"))))
        else:
            writer.write(_json_response(HTTP_404, {"error": f"no {method} {path}"}))
        await writer.drain()

    async def _handle_room_api(
        self, method: str, path: str, headers: dict[str, str], body: bytes, writer: asyncio.StreamWriter,
    ) -> None:
        """A step's run's room tools, as ``_handle_agent_api`` serves the Manager's: only with a run's token that
        still works (its run runs), for its task and seat."""
        ops, header = self._room_ops, headers.get("authorization")
        assert ops is not None
        if not ops.authorized(header):
            writer.write(_json_response(HTTP_401, {"error": "this run's token no longer works: its run ended"}))
        elif path == f"{AGENT_API}tools" and method == "GET":
            writer.write(_json_response(HTTP_200, {"tools": ops.listing(header)}))
        elif (m := _RE_AGENT_OP.match(path)) and method == "POST":
            try:
                arguments = json.loads(body.decode("utf-8")) if body.strip() else {}
            except (json.JSONDecodeError, UnicodeDecodeError):
                arguments = None
            writer.write(_json_response(HTTP_200, await ops.call(header, m.group(1), arguments)))
        else:
            writer.write(_json_response(HTTP_404, {"error": f"no {method} {path}"}))
        await writer.drain()

    async def _handle_planner_api(
        self, method: str, path: str, headers: dict[str, str], body: bytes, writer: asyncio.StreamWriter,
    ) -> None:
        """A task's Planner's tools, as ``_handle_agent_api`` serves the Manager's: only with a Planner's token that
        still works (its Planner runs), for that task."""
        ops, header = self._planner_ops, headers.get("authorization")
        assert ops is not None
        if not ops.authorized(header):
            writer.write(_json_response(HTTP_401, {"error": "this Planner's token no longer works: its task ended or "
                                                            "a new Planner took over"}))
        elif path == f"{AGENT_API}tools" and method == "GET":
            writer.write(_json_response(HTTP_200, {"tools": await ops.listing(header)}))
        elif (m := _RE_AGENT_OP.match(path)) and method == "POST":
            try:
                arguments = json.loads(body.decode("utf-8")) if body.strip() else {}
            except (json.JSONDecodeError, UnicodeDecodeError):
                arguments = None
            writer.write(_json_response(HTTP_200, await ops.call(header, m.group(1), arguments)))
        else:
            writer.write(_json_response(HTTP_404, {"error": f"no {method} {path}"}))
        await writer.drain()

    async def _answer(
        self,
        writer: asyncio.StreamWriter,
        status: str,
        request: Callable[[], Awaitable[Any]],
    ) -> None:
        """Answer with ``request()``'s result, or with the ``RequestError`` it raised."""
        try:
            result = await request()
        except projects.RequestError as exc:
            status, result = exc.status, {"error": str(exc)}
        writer.write(_json_response(status, _serialize(result)))
        await writer.drain()


    async def _discard_workspace(self, task_id: str, workspace_id: int) -> dict[str, Any]:
        """The user drops a step worktree a task that is not running keeps: its directory and branch go. The task's
        row is locked meanwhile, so a restart waits until the worktree is gone and its Planner reads it so."""
        from ..db import activity as db_activity, connection as db_connection, step_workspaces as db_step_workspaces
        from ..db.tasks import ACTIVE_STATUSES
        from . import step_workspaces

        async with db_connection.transaction() as conn:
            status = await db_step_workspaces.lock_task(conn, task_id)
            row = await db_step_workspaces.get(workspace_id, pool=conn)
            if status is None or row is None or row["task_id"] != task_id:
                raise projects.RequestError(HTTP_404, "no such step worktree in this task")
            if status in ACTIVE_STATUSES or self._task_router.has_planner(task_id):
                raise projects.RequestError(HTTP_409, "stop the task before discarding its step worktrees")
            try:
                workspace = await step_workspaces.discard(step_workspaces.StepWorkspace.from_row(row))
            except step_workspaces.IllegalTransition as exc:
                raise projects.RequestError(HTTP_409, str(exc)) from None
            await db_step_workspaces.save(workspace.row(), pool=conn)
        await db_activity.insert_activity(
            task_id=task_id, kind="phase",
            summary=f"Discarded a step worktree from the task page: branch {workspace.branch} and its directory "
                    "removed"[:500])
        return {"workspace": workspace.view()}

    async def _checks_now(self) -> list[dict[str, str]]:
        """The startup self-check, with the runtimes' own logins read now: a run may have found one forked since."""
        now = {check.get("id"): (await self_check.LOGIN_CHECKS[check["id"]]()).to_dict()
               for check in self._self_checks if check.get("id") in self_check.LOGIN_CHECKS}
        return [now.get(check.get("id"), check) for check in self._self_checks]

    async def _status_result(self, headers: Mapping[str, str]) -> dict[str, Any]:
        agents = self._supervisor.agent_status()
        web_health = self._web_adapter.healthcheck()
        try:
            from ..db.tasks import status_counts
            counts = overview.group_counts(await status_counts())
        except Exception:
            logger.exception("DB unavailable for status task counts")
            counts = overview.group_counts([])
        status = {
            "status": "running",
            "agents": agents,
            # Every task counted; "running" is every task in progress (one waiting for the user too).
            "tasks": {
                "total": counts["total"],
                "running": counts["in_progress"] + counts["needs_you"],
                "completed": counts["completed"],
            },
            "channels": {"web": web_health},
            "checks": await self._checks_now(),
            "can_sign_out": self._can_sign_out(headers),
        }
        if self._managers is not None:
            # Whether tool calls are refused by their turn binding, and the mismatches seen (chat-stop-v2 §10).
            status["turn_binding"] = self._managers.binding_report()
            status["managers"] = [
                {"id": agent.agent_id, "name": agent.name, **self._managers.state(agent.agent_id),
                 "turn_binding": self._managers.ops(agent.agent_id).binding_report()}
                for agent in self._managers.agents() if self._managers.ops(agent.agent_id) is not None]
        elif self._manager_ops is not None:
            status["turn_binding"] = self._manager_ops.binding_report()
        return status

    async def _handle_list_tasks(self, query: dict[str, str], writer: asyncio.StreamWriter) -> None:
        """A page of tasks, newest first, filtered by project, schedule, group and status: ``limit`` of them
        (``TASKS_PAGE`` by default, at most ``TASKS_PAGE_MAX``) older than the task ``before``; ``next_before``
        continues the list (null at its end) and ``counts`` has how many tasks each status holds under the other
        filters."""
        from ..db import tasks as db_tasks

        async def refuse(error: str) -> None:
            writer.write(_json_response(HTTP_400, {"error": error}))
            await writer.drain()

        try:
            group = TaskGroup(query["group"]) if query.get("group") else None
        except ValueError:
            await refuse(f"group must be one of {', '.join(g.value for g in TaskGroup)}")
            return
        status = query.get("status") or None
        if status is not None and status not in db_tasks.STATUS_GROUPS:
            await refuse(f"status must be one of {', '.join(db_tasks.STATUS_GROUPS)}")
            return
        raw = query.get("limit") or str(TASKS_PAGE)
        if not raw.isdigit() or not 1 <= int(raw) <= TASKS_PAGE_MAX:
            await refuse(f"limit must be a whole number from 1 to {TASKS_PAGE_MAX}")
            return
        limit, before = int(raw), query.get("before") or None
        filters = {"project_id": query.get("project_id") or None, "group": group,
                   "schedule_id": query.get("schedule_id") or None}
        try:
            result = await projects.list_tasks_page(limit=limit, status=status, before=before, **filters)
        except projects.RequestError as exc:
            await refuse(str(exc))
            return
        except Exception:
            logger.exception("DB unavailable for list_tasks")
            writer.write(_json_response(HTTP_500, {"error": "database unavailable"}))
            await writer.drain()
            return
        writer.write(_json_response(HTTP_200, _serialize(result)))
        await writer.drain()

    def _stop_agent(self, message_id: str) -> Any:
        """The Manager whose turn answers ``message_id`` (the default one when none does)."""
        if self._managers is None:
            return self._manager
        found = self._managers.turn_for(message_id)
        return found[0] if found is not None else self._managers.default

    def _manager_for(self, manager_id: str | None) -> Any:
        """The running Manager ``manager_id`` (the default one without it); None when it is not running."""
        if self._managers is None:
            return self._manager
        return self._managers.get(manager_id or "manager")

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

        try:
            target = await resolve_chat_target(data.get("target"), self._task_router, self._managers)
        except ChatTargetError as exc:
            writer.write(_json_response(exc.status, {"error": str(exc)}))
            await writer.drain()
            return

        new_session_profile_id = data.get("new_session_profile_id")
        if new_session_profile_id is not None:
            if target.agent_type != "manager" or not is_new_session_command(text):
                writer.write(_json_response(
                    HTTP_400, {"error": "new_session_profile_id goes only with /new to the Manager"}))
                await writer.drain()
                return
            try:
                await profile_registry.check_selection(new_session_profile_id, ["manager"])
            except profile_registry.RegistryError as exc:
                writer.write(_json_response(HTTP_400, {"error": str(exc)}))
                await writer.drain()
                return

        session_id = data.get("session_id", "")
        message_id = data.get("message_id", "")
        if target.agent_type == "manager" and session_id and self._managers is not None:
            owner = await self._thread_owner_for_send(session_id, target.agent_id)
            if owner != target.agent_id:
                writer.write(await self._foreign_thread(owner))
                await writer.drain()
                return
        result = await self._deliver_chat(
            text=text, target=target, session_id=session_id or data.get("session_id"), message_id=message_id,
            language=data.get("language", "zh"), goal_id=data.get("goal_id"), attachments=data.get("attachments"),
            new_session_profile_id=new_session_profile_id)
        writer.write(_json_response(HTTP_200, result))
        await writer.drain()

    async def _deliver_chat(
        self, *, text: str, target: AgentAddress, session_id: str | None, message_id: str | None,
        language: str = "zh", goal_id: Any = None, attachments: Any = None,
        new_session_profile_id: str | None = None, client: Mapping[str, str] | None = None,
    ) -> dict[str, Any]:
        """Send ``text`` to ``target``: the one way a chat message goes in, for the dashboard and for an API client
        (``client``: its ``id`` and ``name``). Stores it as the Planner's supplement and as the thread's user message
        first; the adapter's answer has the ``correlation_id`` its reply streams under."""
        async def deliver(supplement_id: int | None) -> dict[str, Any]:
            if session_id and message_id:
                try:
                    from ..db.chat import save_message
                    await save_message(
                        session_id=session_id, message_id=message_id,
                        role="user", content=text,
                    )
                except Exception:
                    logger.debug("failed to persist user chat message", exc_info=True)

            return await self._web_adapter.handle_user_message(
                text=text,
                session_id=session_id,
                goal_id=goal_id,
                language=language,
                attachments=attachments,
                target=target,
                message_id=message_id or None,
                supplement_id=supplement_id,
                **({"new_session_profile_id": new_session_profile_id} if new_session_profile_id is not None else {}),
                **({"client": client} if client is not None else {}),
            )

        if target.agent_type == "planner":
            return await projects.add_task_supplement(
                target.agent_id.removeprefix("planner-"), text, via=projects.VIA_CHAT,
                wake=deliver, best_effort=True)
        return await deliver(None)

    async def _foreign_thread(self, owner: str) -> bytes:
        """The 409 for a thread of Manager ``owner`` asked for under another Manager: whose it is, by name."""
        error = await self._foreign_thread_error(owner)
        return _json_response(HTTP_409, {"error": str(error), "manager_id": owner})

    async def _foreign_thread_error(self, owner: str) -> OpError:
        from ..db import managers as db_managers
        running = self._managers.get(owner) if self._managers is not None else None
        row = None if running is not None else await db_managers.get_manager(owner, include_deleted=True)
        name = running.name if running is not None else row["name"] if row and row.get("deleted_at") is None else None
        error = (f"this conversation belongs to Manager {name}: open it there or start a new one" if name is not None
                 else "this conversation belongs to a deleted Manager: start a new one")
        return OpError(ErrorCode.CONFLICT, error, details={"manager_id": owner})

    async def _thread_owner_elsewhere(self, session_id: str, manager_id: str) -> str | None:
        """The Manager other than ``manager_id`` that thread ``session_id`` belongs to (an old thread, with messages
        and no row, is the default Manager's); None for its own thread or a new one (no row, no messages)."""
        from ..db import chat as db_chat
        owner = await db_chat.thread_owner(session_id)
        if owner is None and await db_chat.has_messages(session_id):
            owner = "manager"
        return owner if owner is not None and owner != manager_id else None

    async def _thread_owner_for_send(self, session_id: str, wanted: str) -> str:
        """The Manager that owns the thread, claiming it for ``wanted`` when it is new; an old thread (messages, no
        row) is the default Manager's."""
        from ..db import chat as db_chat
        owner = await db_chat.thread_owner(session_id)
        if owner is None:
            owner = await db_chat.claim_thread(
                session_id, "manager" if await db_chat.has_messages(session_id) else wanted)
        return owner

    async def _handle_sse_events(
        self,
        query: dict[str, str],
        writer: asyncio.StreamWriter,
    ) -> None:
        cid = query.get("cid", "")
        if not cid and query.get("session"):
            if owner := await self._thread_owner_elsewhere(query["session"], query.get("manager") or "manager"):
                writer.write(await self._foreign_thread(owner))
                await writer.drain()
                return
            await self._handle_chat_session_events(query["session"], writer)
            return
        if not cid:
            writer.write(_json_response(HTTP_400, {"error": "cid or session is required"}))
            await writer.drain()
            return

        sse_headers = (
            "HTTP/1.1 200 OK\r\n"
            "Content-Type: text/event-stream\r\n"
            "Cache-Control: no-cache\r\n"
            "Connection: keep-alive\r\n"
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

    async def _handle_chat_session_events(self, session_id: str, writer: asyncio.StreamWriter) -> None:
        """SSE stream of what reaches an open chat outside any message's stream, until the page leaves."""
        events = self._web_adapter.iter_chat_events(session_id)
        writer.write((
            "HTTP/1.1 200 OK\r\n"
            "Content-Type: text/event-stream\r\n"
            "Cache-Control: no-cache\r\n"
            "Connection: keep-alive\r\n"
            "X-Accel-Buffering: no\r\n"
            "\r\n"
        ).encode("utf-8"))
        try:
            await writer.drain()
            async for event in events:
                payload = json.dumps(event.get("payload", {}), ensure_ascii=False)
                writer.write(f"event: {event.get('kind', 'message')}\ndata: {payload}\n\n".encode("utf-8"))
                await writer.drain()
        except (ConnectionError, BrokenPipeError):
            pass
        finally:
            await events.aclose()

    async def _task_config_result(self, task_id: str, data: dict[str, Any]) -> dict[str, Any]:
        """Edit an unstarted task's configuration and retain requirement history; no HTTP writer."""
        from ..db.tasks import update_task_config, get_task

        task = await get_task(task_id)
        if task is None:
            raise projects.RequestError(HTTP_404, "task not found")
        if task.get("status") != "init":
            raise projects.RequestError(HTTP_400, "can only edit config when status is init")
        config = data.get("config")
        if config is not None and not isinstance(config, dict):
            raise projects.RequestError(HTTP_400, "config must be a JSON object")
        work_dir = None
        if "work_dir" in data:
            from .tiers.task import resolve_work_dir
            try:
                work_dir = resolve_work_dir(data["work_dir"])
            except ValueError as exc:
                raise projects.RequestError(HTTP_400, str(exc)) from None
        requirement = data.get("requirement")
        if isinstance(requirement, str):
            projects.check_requirement(requirement)
        if isinstance(requirement, str) and requirement.strip() and requirement != task.get("requirement"):
            from ..db.task_changes import edit_requirement
            if await edit_requirement(task_id, requirement, via=projects.VIA_TASK_PAGE, delivered=True) is None:
                raise projects.RequestError(HTTP_404, "task not found")
        if (work_dir is not None or config is not None) and not await update_task_config(
                task_id, work_dir=work_dir, config=config):
            raise OpError(ErrorCode.UNAVAILABLE, "task configuration update failed")
        updated = await get_task(task_id)
        if updated is None:
            raise projects.RequestError(HTTP_404, "task not found")
        return _serialize({"task_id": updated["id"], "requirement": updated.get("requirement", ""),
                           "work_dir": updated.get("work_dir", ""), "config": updated.get("config", {}),
                           "status": updated.get("status", "")})


    async def _delete_task(self, task_id: str) -> dict[str, Any]:
        """Delete a stopped task, preserving the established workspace/session cleanup order."""
        from ..db.tasks import ACTIVE_STATUSES, delete_task, get_task

        task_id = task_id.strip()
        task = await get_task(task_id)
        if task is None:
            raise projects.RequestError(HTTP_404, "task not found")
        if task.get("status", "") in ACTIVE_STATUSES:
            raise projects.RequestError(HTTP_400, "stop the task before deleting")

        from . import step_workspaces, task_workspaces
        try:
            await step_workspaces.remove_task(task_id)
        except Exception:
            logger.warning("could not remove the step worktrees of task %s", task_id, exc_info=True)
        try:
            # After the steps' worktrees, which are of the task's: what it left goes onto its branch, which stays.
            await task_workspaces.finish(task_id, deleted=True)
        except task_workspaces.TaskWorkspaceError as exc:
            raise projects.RequestError(
                HTTP_409, f"the task's worktree was kept, so nothing is lost: {exc}") from None
        if self._runtime_root is not None:
            from .room import remove_run_sessions
            remove_run_sessions(self._runtime_root, task_id)
        await delete_task(task_id)
        return {"deleted": task_id}

    # --- Task detail handlers ---

    async def _handle_task_detail(
        self, task_id: str, writer: asyncio.StreamWriter,
    ) -> None:
        try:
            result = await projects.task_detail(task_id)
        except projects.RequestError as exc:
            writer.write(_json_response(exc.status, {"error": str(exc)}))
        except Exception:
            logger.exception("DB unavailable for task_detail %s", task_id)
            writer.write(_json_response(HTTP_500, {"error": "database unavailable"}))
        else:
            writer.write(_json_response(HTTP_200, _serialize(result)))
        await writer.drain()

    @staticmethod
    async def _task_workspace(task: dict[str, Any]) -> dict[str, Any]:
        return await projects.task_workspace_view(task)

    @staticmethod
    async def _task_workspaces(task_id: str, steps: list[dict[str, Any]], *,
                               in_worktree: bool = False) -> dict[str, Any] | None:
        return await projects.step_workspaces_view(task_id, steps, in_worktree=in_worktree)

    # --- Agent settings (the defaults every agent inherits) ---

    async def _write_layer(
        self, body: bytes, update: Callable[[dict[str, Any]], Awaitable[dict[str, Any]]],
    ) -> dict[str, Any]:
        """``update`` a role's layer with the body; when it set a concurrency limit, start what it lets start."""
        data = projects.json_object(body)
        result = await update(data)
        if "max_concurrency" in data:
            await self._recheck_concurrency()
        return result

    async def _apply_import(self, imports: config_imports.ConfigImports, import_id: str, body: bytes = b"", *,
                            owner: str | None = None) -> Any:
        """Apply an import; its layers may raise a concurrency limit."""
        if body:
            raise config_transfer.ConfigFileError("apply takes no request body; the server recomputes the import plan")
        result = await imports.apply(import_id, owner=owner)
        await self._recheck_concurrency()
        return result

    async def _recheck_concurrency(self) -> None:
        """Start the runs that wait for room a raised limit now gives; a lowered limit stops nothing."""
        await recheck_limits(self._worker_pool)

    async def _handle_agent_settings(
        self, writer: asyncio.StreamWriter, request: Callable[[], Awaitable[dict[str, Any]]],
    ) -> None:
        """Answer with the agent settings ``request()`` returns; a refused value is 400, an unknown role or
        project 404."""
        try:
            result = await request()
        except (agent_settings.SettingsError, projects.RequestError) as exc:
            status, result = getattr(exc, "status", HTTP_400), {"error": str(exc)}
        except agent_settings.SettingsNotFound as exc:
            status, result = HTTP_404, {"error": str(exc)}
        except Exception as exc:
            logger.exception("agent settings request failed")
            status, result = HTTP_500, {"error": str(exc)}
        else:
            status = HTTP_200
        writer.write(_json_response(status, _serialize(result)))
        await writer.drain()

    async def _handle_registry(
        self, writer: asyncio.StreamWriter, status: str, request: Callable[[], Awaitable[Any]],
    ) -> None:
        """Answer with ``request()``'s result; a refused request is 400, an unknown runtime, provider or Agent
        Profile 404, and a name in use or a row still referred to 409."""
        try:
            result = await request()
        except (profile_registry.RegistryError, projects.RequestError) as exc:
            status, result = getattr(exc, "status", HTTP_400), {"error": str(exc)}
        except profile_registry.RegistryNotFound as exc:
            status, result = HTTP_404, {"error": str(exc)}
        except profile_registry.RegistryConflict as exc:
            status, result = HTTP_409, {"error": str(exc)}
        except Exception:
            logger.exception("registry request failed")
            status, result = HTTP_500, {"error": "the request failed; see the server log"}
        writer.write(_json_response(status, _serialize(result)))
        await writer.drain()

    # --- A project's knowledge (the documents its task agents read on demand) ---

    async def _handle_knowledge(
        self, writer: asyncio.StreamWriter, status: str, request: Callable[[Path], Awaitable[Any]],
    ) -> None:
        """Answer with ``request(runtime_root)``'s result; a refused request is 400, an unknown project or
        document 404."""
        try:
            if self._runtime_root is None:
                raise RuntimeError("the server was started without a runtime root")
            result = await request(self._runtime_root)
        except (knowledge.KnowledgeError, projects.RequestError) as exc:
            status, result = getattr(exc, "status", HTTP_400), {"error": str(exc)}
        except knowledge.KnowledgeNotFound as exc:
            status, result = HTTP_404, {"error": str(exc)}
        except Exception:
            logger.exception("knowledge request failed")
            status, result = HTTP_500, {"error": "the knowledge request failed; see the server log"}
        writer.write(_json_response(status, _serialize(result)))
        await writer.drain()

    # --- Configuration export ---

    async def _handle_config_export(self, query: dict[str, str], writer: asyncio.StreamWriter) -> None:
        """Stream the configuration file as a download, piece by piece from one read-only snapshot.

        ``projects=1`` and ``secrets=1`` are ``looper config export``'s
        ``--include-projects`` and ``--include-secrets``. The status line is
        sent before the first piece, so a failure after it ends the download
        early, which leaves a file that is not valid JSON.
        """
        import datetime

        from ..db.connection import get_pool
        from .config_transfer import export_chunks

        include_projects, include_secrets = query.get("projects") == "1", query.get("secrets") == "1"
        name = f"looper-config-{datetime.date.today().isoformat()}.json"
        async with get_pool().acquire() as conn, conn.transaction(isolation="repeatable_read", readonly=True):
            chunks = export_chunks(conn, include_projects=include_projects, include_secrets=include_secrets)
            first = await anext(chunks)
            writer.write(
                f"HTTP/1.1 {HTTP_200}\r\n"
                "Content-Type: application/json; charset=utf-8\r\n"
                f'Content-Disposition: attachment; filename="{name}"\r\n'
                "Cache-Control: no-store\r\n"
                "Connection: close\r\n"
                "\r\n".encode() + first.encode("utf-8")
            )
            async for chunk in chunks:
                writer.write(chunk.encode("utf-8"))
                await writer.drain()
        await writer.drain()

    async def _handle_config_import(
        self, writer: asyncio.StreamWriter, status: str,
        request: Callable[[config_imports.ConfigImports], Awaitable[Any]],
    ) -> None:
        """Answer with ``request(imports)``'s result: a file that is not a usable configuration file is 400, an
        unknown or expired import 404, and too many pending, one being applied or a schema awaiting
        ``looper serve``'s migration 409."""
        try:
            if self._config_imports is None:
                raise RuntimeError("the server was started without a runtime root")
            result = await request(self._config_imports)
        except config_transfer.ConfigFileError as exc:
            status, result = HTTP_400, {"error": str(exc)}
        except config_imports.ImportNotFound as exc:
            status, result = HTTP_404, {"error": str(exc)}
        except (config_imports.ImportConflict, config_transfer.SchemaChangePending) as exc:
            status, result = HTTP_409, {"error": str(exc)}
        except (asyncio.TimeoutError, ConnectionError, asyncio.IncompleteReadError):
            raise  # the client stopped sending; there is no one to answer
        except Exception:
            logger.exception("config import request failed")
            status, result = HTTP_500, {"error": "the import failed; see the server log"}
        writer.write(_json_response(status, _serialize(result)))
        await writer.drain()

    def _password_record(self) -> operator_auth.PasswordRecord | None:
        if self._runtime_root is None:
            return None
        try:
            info = os.stat(self._runtime_root / operator_auth.PASSWORD_FILE, follow_symlinks=False)
            key = (info.st_ino, info.st_size, info.st_mtime_ns, info.st_mode, info.st_uid)
        except FileNotFoundError:
            self._password_key, self._password, self._password_error = None, None, None
            return None
        except OSError as exc:
            raise operator_auth.OperatorTokenError(f"cannot stat password file: {exc}") from None
        if key != self._password_key:
            self._password, self._password_error = None, None
            try:
                self._password = operator_auth.load_password(self._runtime_root)
            except operator_auth.OperatorTokenError as exc:
                self._password_error = exc
                logger.error("%s", exc)
            self._password_key = key
        if self._password_error:
            raise self._password_error
        return self._password

    def _session_cookie_name(self) -> str:
        return f"looper_session_{self._port}"

    def _is_operator(self, headers: Mapping[str, str], *, browser: bool = False) -> bool:
        credentials = {key: value for key, value in headers.items() if key != "authorization"} if browser else headers
        if operator_auth.is_operator(credentials, token=self._operator_token, port=self._port):
            return True
        return self._has_password_session(headers)

    def _has_password_session(self, headers: Mapping[str, str]) -> bool:
        session_id = operator_auth.cookie(headers, self._session_cookie_name())
        if session_id is None:
            return False
        try:
            record = self._password_record()
        except operator_auth.OperatorTokenError:
            return False
        return record is not None and self._sessions.valid(session_id, record.hash)

    def _can_sign_out(self, headers: Mapping[str, str]) -> bool:
        """True for a browser that signed in with the password and holds no token cookie: signing out ends it. A
        token session (the local browser's auto-login) has nothing to sign out of: /login would sign it in again."""
        browser = {key: value for key, value in headers.items() if key != "authorization"}
        return (self._has_password_session(headers)
                and not operator_auth.is_operator(browser, token=self._operator_token, port=self._port))

    async def _sign_in_response(self, writer: asyncio.StreamWriter, status: str, body: str = "", *,
                                headers: Mapping[str, str] | None = None, cookies: tuple[str, ...] = ()) -> None:
        data = body.encode("utf-8")
        fields = {**SIGN_IN_HEADERS, **(headers or {})}
        writer.write((f"HTTP/1.1 {status}\r\nContent-Type: text/html; charset=utf-8\r\n"
                      f"Content-Length: {len(data)}\r\n"
                      + "".join(f"{key}: {value}\r\n" for key, value in fields.items())
                      + "".join(f"Set-Cookie: {cookie}\r\n" for cookie in cookies)
                      + "\r\n").encode() + data)
        await writer.drain()

    async def _login_page(self, writer: asyncio.StreamWriter, status: str = HTTP_200, error: str = "", *,
                          headers: Mapping[str, str] | None = None) -> None:
        unusable = False
        try:
            record = self._password_record()
        except operator_auth.OperatorTokenError:
            record = None
            unusable = True
            status, error = HTTP_503, "the server's password file is unusable; see the server log"
        form = ("<form method=\"post\" action=\"/login\"><label>User name"
                "<input name=\"username\" value=\"admin\" autocomplete=\"username\" required></label>"
                "<label>Password<input name=\"password\" type=\"password\" autocomplete=\"current-password\" required>"
                "</label><button type=\"submit\">Sign in</button></form>") if record else (
                "<p>No password is set. On the server's machine run <code>looper auth set-password</code>, "
                "or open the dashboard there.</p>" if not unusable else "")
        page = ("<!doctype html><html lang=\"en\"><meta charset=\"utf-8\">"
                "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\"><title>Looper</title>"
                "<style>:root{color-scheme:light dark;font:16px system-ui;background:#f4f5f7;color:#202124}"
                "body{max-width:24rem;margin:12vh auto;padding:2rem}form,label{display:grid;gap:.6rem}"
                "form{gap:1.2rem}input,button{font:inherit;padding:.7rem;border:1px solid #888;border-radius:.4rem}"
                ".error{color:#b3261e}@media(prefers-color-scheme:dark){:root{background:#18191b;color:#eee}"
                ".error{color:#ffb4ab}}</style><h1>Looper</h1>"
                + (f"<p class=\"error\">{html.escape(error)}</p>" if error else "") + form + "</html>")
        await self._sign_in_response(writer, status, page, headers=headers)

    async def _handle_sign_in(self, method: str, path: str, headers: Mapping[str, str], body: bytes,
                              peer_host: str | None, writer: asyncio.StreamWriter) -> None:
        if method == "GET" and path == "/login":
            cookies = ()
            signed_in = self._is_operator(headers, browser=True)
            if not signed_in and self._operator_token and operator_auth.signs_in(headers, peer_host=peer_host):
                signed_in = True
                cookies = (operator_auth.set_cookie(self._operator_token, port=self._port),)
            if signed_in:
                await self._sign_in_response(writer, HTTP_303, headers={"Location": "/"}, cookies=cookies)
            else:
                await self._login_page(writer)
            return
        if method != "POST":
            await self._sign_in_response(writer, HTTP_405)
            return
        origin = urllib.parse.urlsplit(headers.get("origin") or "")
        host = headers.get("host", "").lower()
        scheme = "https" if (self._dashboard_url or "").startswith("https://") else "http"
        if not host or origin.scheme != scheme or origin.netloc.lower() != host:
            await self._sign_in_response(writer, HTTP_403)
            return
        if path == "/logout":
            self._sessions.end(operator_auth.cookie(headers, self._session_cookie_name()) or "")
            cookies = tuple(f"{name}=; Path=/; Max-Age=0; HttpOnly; SameSite=Strict" for name in (
                self._session_cookie_name(), operator_auth.cookie_name(self._port)))
            await self._sign_in_response(writer, HTTP_303, headers={"Location": "/login"}, cookies=cookies)
            return
        # One attempt at a time, so parallel guesses can't all pass the limiter before a failure counts.
        async with self._sign_in_lock:
            retry_after = self._login_limiter.retry_after(peer_host)
            if retry_after:
                logger.warning("sign-in from %s locked", peer_host)
                await self._login_page(writer, HTTP_429, "Please wait before signing in again.",
                                       headers={"Retry-After": str(retry_after)})
                return
            if headers.get("content-type", "").split(";", 1)[0].strip().lower() != "application/x-www-form-urlencoded":
                await self._sign_in_response(writer, HTTP_400)
                return
            try:
                fields = dict(urllib.parse.parse_qsl(body.decode("utf-8"), keep_blank_values=True))
            except UnicodeError:
                await self._sign_in_response(writer, HTTP_400)
                return
            try:
                record = self._password_record()
                if record is None:
                    await self._login_page(writer, HTTP_401)
                    return
                matched = await asyncio.to_thread(operator_auth.verify, record, fields.get("username", ""),
                                                  fields.get("password", ""))
            except (operator_auth.OperatorTokenError, ValueError) as exc:
                logger.error("password file is unusable: %s", exc)
                await self._login_page(writer, HTTP_503,
                                       "the server's password file is unusable; see the server log")
                return
            if not matched:
                self._login_limiter.failure(peer_host)
                logger.warning("sign-in from %s failed", peer_host)
                await self._login_page(writer, HTTP_401, "wrong user name or password")
                return
            self._login_limiter.success(peer_host)
            logger.info("sign-in from %s succeeded", peer_host)
            session_id = self._sessions.create(record.user, record.hash)
            cookie = (f"{self._session_cookie_name()}={session_id}; Path=/; HttpOnly; SameSite=Strict; "
                      f"Max-Age={operator_auth.SESSION_TTL_SECONDS}")
            if (self._dashboard_url or "").startswith("https://"):
                cookie += "; Secure"
            await self._sign_in_response(writer, HTTP_303, headers={"Location": "/"}, cookies=(cookie,))

    # --- Static file serving ---

    async def _serve_static(
        self, path: str, writer: asyncio.StreamWriter, *, headers: Mapping[str, str] | None = None,
        query: Mapping[str, str] | None = None, peer_host: str | None = None,
    ) -> None:
        page = path in ("/", "/index.html")
        file_path = self._static_dir / ("index.html" if path == "/" else path.lstrip("/"))

        if not file_path.is_file():
            index_path = self._static_dir / "index.html"
            if index_path.is_file():
                file_path = index_path
                page = True
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

        if page and not self._is_operator(headers or {}, browser=True) and not (
            self._operator_token and operator_auth.signs_in(headers or {}, peer_host=peer_host)
        ):
            await self._login_page(writer)
            return

        content_type, _ = mimetypes.guess_type(str(file_path))
        if content_type is None:
            content_type = "application/octet-stream"

        data = file_path.read_bytes()
        cookie = ""
        if page and self._operator_token and operator_auth.signs_in(headers or {}, peer_host=peer_host):
            cookie = f"Set-Cookie: {operator_auth.set_cookie(self._operator_token, port=self._port)}\r\n"
        policy = "Referrer-Policy: same-origin\r\n" if page else ""  # the Sign out form POST needs a real Origin
        response_headers = (
            f"HTTP/1.1 {HTTP_200}\r\n"
            f"Content-Type: {content_type}\r\n"
            f"Content-Length: {len(data)}\r\n"
            f"Cache-Control: {'no-store' if page else 'public, max-age=3600'}\r\n"
            f"{policy}{cookie}"
            f"\r\n"
        )
        writer.write(response_headers.encode("utf-8") + data)
        await writer.drain()
