"""Async HTTP server for the Looper agent system.

Pure stdlib asyncio — zero external dependencies. Serves:
  - REST API endpoints (/api/*)
  - SSE streaming (/api/chat/events: a message's stream by ?cid=, an open chat's by ?session=)
  - Static files from web/dist/ with SPA fallback
"""

from __future__ import annotations

import asyncio
import json
import logging
import mimetypes
import re
import urllib.parse
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from ..db.tasks import TaskGroup, task_group
from . import (
    agent_settings, catalog, catalog_sources, config_imports, config_transfer, knowledge, library, mcp, overview,
    profile_registry, project_skills, projects, request_guard, schedules, self_check,
)
from .broadcast import Broadcaster
from .chat_targets import ChatTargetError, list_chat_targets, resolve_chat_target
from .project_execution import ProjectExecution
from .channels.web import WebChannelAdapter
from .scheduler import Scheduler
from .supervisor import AgentSupervisor
from .tiers.journal import progress_notes
from .tiers.manager import ManagerAgent
from .tiers.task import TaskRouter
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
HTTP_400 = "400 Bad Request"
HTTP_403 = "403 Forbidden"
HTTP_404 = "404 Not Found"
HTTP_405 = "405 Method Not Allowed"
HTTP_409 = "409 Conflict"
HTTP_413 = "413 Content Too Large"
HTTP_500 = "500 Internal Server Error"
HTTP_502 = "502 Bad Gateway"


# The largest request body the server reads. The largest legitimate one is a
# knowledge upload: a document of at most knowledge.DOCUMENT_MAX_BYTES (2 MiB)
# as base64 in JSON is about 2.7 MiB, and as JSON-escaped text up to twice its
# size; 8 MiB leaves room for both. A larger body is refused unread. The one
# exception is a configuration import (CONFIG_IMPORTS_PATH), which streams its
# body to a file and takes up to config_imports.UPLOAD_MAX_BYTES.
REQUEST_BODY_MAX_BYTES = 4 * knowledge.DOCUMENT_MAX_BYTES
CONFIG_IMPORTS_PATH = "/api/config/imports"


def _json_response(status: str, body: Any) -> bytes:
    payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
    headers = (
        f"HTTP/1.1 {status}\r\n"
        f"Content-Type: application/json; charset=utf-8\r\n"
        f"Content-Length: {len(payload)}\r\n"
        f"\r\n"
    )
    return headers.encode("utf-8") + payload


def _no_content() -> bytes:
    # Same-origin requests never preflight, so an allowed OPTIONS gets no CORS headers.
    return b"HTTP/1.1 204 No Content\r\nContent-Length: 0\r\n\r\n"


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
        self_checks: list[dict[str, str]] | None = None,
        claude_bin: str = "claude",
        scheduler: Scheduler | None = None,
    ) -> None:
        self._host = host
        self._port = port
        self._web_adapter = web_adapter
        self._task_router = task_router
        # Told, with the TaskRouter, to start what a raised concurrency limit lets start.
        self._worker_pool = worker_pool
        # Whether it is in a turn, for the overview.
        self._manager = manager
        self._supervisor = supervisor
        self._broadcaster = broadcaster or Broadcaster()
        self._static_dir = static_dir or default_static_dir()
        # Where the agents' files live: project knowledge is materialized under it.
        self._runtime_root = runtime_root
        # The startup self-check (self_check.Check.to_dict), reported on /api/status.
        self._self_checks = self_checks or []
        # The Claude Code the server runs: what a Claude Code runtime without an executable of its own probes.
        self._claude_bin = claude_bin
        # What "Run now" on a schedule fires through.
        self._scheduler = scheduler
        # Configuration files uploaded for import, waiting to be applied (under the runtime root).
        self._config_imports = config_imports.ConfigImports(runtime_root) if runtime_root else None
        self._server: asyncio.Server | None = None

    async def start(self) -> None:
        if self._config_imports:
            self._config_imports.clear()  # what a previous server left pending
        self._server = await asyncio.start_server(
            self._handle_connection, self._host, self._port
        )
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
            query = dict(urllib.parse.parse_qsl(parsed.query))

            # Refused before its body is read: another site's page cannot even upload.
            refused = request_guard.refusal(headers, bind_host=self._host)
            if refused:
                logger.warning("refused %s %s: %s", method, path, refused)
                writer.write(_json_response(HTTP_403, {"error": refused}))
                await writer.drain()
                writer.close()
                return

            streamed = method == "POST" and path == CONFIG_IMPORTS_PATH
            cap = config_imports.UPLOAD_MAX_BYTES if streamed else REQUEST_BODY_MAX_BYTES
            body = b""
            try:
                content_length = int(headers.get("content-length", "0"))
            except ValueError:
                content_length = -1
            if content_length < 0:
                writer.write(_json_response(HTTP_400, {"error": "bad Content-Length"}))
                await writer.drain()
                return
            if content_length > cap:
                writer.write(_json_response(HTTP_413, {
                    "error": f"the request body has {content_length} bytes; "
                             f"{'a configuration import' if streamed else 'the server'} takes at most "
                             f"{cap // (1024 * 1024)} MiB ({cap} bytes)",
                }))
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

            if method == "OPTIONS":
                writer.write(_no_content())
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

    _RE_TASK_DETAIL = re.compile(r"^/api/tasks/(task-[a-zA-Z0-9]+)$")
    _RE_CATALOG_ITEM = re.compile(r"^/api/catalog/([a-z]+-[a-zA-Z0-9]+)$")
    _RE_INSTALL = re.compile(r"^/api/installs/(inst-[a-zA-Z0-9]+)$")
    _RE_TASK_ACTIVITY = re.compile(r"^/api/tasks/(task-[a-zA-Z0-9]+)/activity$")
    _RE_TASK_ACTIVITY_STREAM = re.compile(r"^/api/tasks/(task-[a-zA-Z0-9]+)/activity/stream$")
    _RE_TASK_ACTIVITY_SEQ = re.compile(r"^/api/tasks/(task-[a-zA-Z0-9]+)/activity/(\d+)$")
    _RE_TASK_CONVERSATION = re.compile(r"^/api/tasks/(task-[a-zA-Z0-9]+)/conversation$")
    _RE_ROLE_CONFIG = re.compile(r"^/api/roles/([a-zA-Z0-9_-]+)$")
    _RE_ROLE_CONFIG_RESET = re.compile(r"^/api/roles/([a-zA-Z0-9_-]+)/reset-prompt$")
    _RE_LLM_RECORD = re.compile(r"^/api/llm-records/(\d+)$")
    _RE_LLM_RECORD_EVENTS = re.compile(r"^/api/llm-records/(\d+)/events$")
    _RE_AGENT_USAGE = re.compile(r"^/api/agents/([a-zA-Z0-9_-]+)/usage$")
    _RE_CHANNEL_CONFIG = re.compile(r"^/api/channel-configs/([a-zA-Z0-9_-]+)$")
    _RE_TASK_CONFIG = re.compile(r"^/api/tasks/(task-[a-zA-Z0-9]+)/config$")
    _RE_TASK_PROJECT = re.compile(r"^/api/tasks/(task-[a-zA-Z0-9]+)/project$")
    _RE_TASK_CONTINUE = re.compile(r"^/api/tasks/(task-[a-zA-Z0-9]+)/continue$")
    _RE_TASK_START = re.compile(r"^/api/tasks/(task-[a-zA-Z0-9]+)/start$")
    _RE_PROJECT = re.compile(r"^/api/projects/(proj-[a-zA-Z0-9]+)$")
    _RE_PROJECT_KNOWLEDGE = re.compile(r"^/api/projects/(proj-[a-zA-Z0-9]+)/knowledge$")
    _RE_PROJECT_DOCUMENT = re.compile(r"^/api/projects/(proj-[a-zA-Z0-9]+)/knowledge/(doc-[a-zA-Z0-9]+)$")
    _RE_PROJECT_DRAFTS = re.compile(r"^/api/projects/(proj-[a-zA-Z0-9]+)/knowledge/drafts$")
    _RE_PROJECT_DRAFT = re.compile(r"^/api/projects/(proj-[a-zA-Z0-9]+)/knowledge/drafts/(draft-[a-zA-Z0-9]+)$")
    _RE_PROJECT_DRAFT_CONFIRM = re.compile(
        r"^/api/projects/(proj-[a-zA-Z0-9]+)/knowledge/drafts/(draft-[a-zA-Z0-9]+)/confirm$")
    _RE_PROJECT_INSTALLS = re.compile(r"^/api/projects/(proj-[a-zA-Z0-9]+)/installs$")
    _RE_PROJECT_AGENT_SETTINGS = re.compile(r"^/api/projects/(proj-[a-zA-Z0-9]+)/agent-settings$")
    _RE_PROJECT_ROLE_SETTINGS = re.compile(r"^/api/projects/(proj-[a-zA-Z0-9]+)/agent-settings/roles/([a-zA-Z0-9_-]+)$")
    _RE_ROLE_SETTINGS = re.compile(r"^/api/agent-settings/roles/([a-zA-Z0-9_-]+)$")
    _RE_RUNTIME_PROBE = re.compile(r"^/api/runtimes/(rt-[a-zA-Z0-9-]+)/probe$")
    _RE_PROVIDER = re.compile(r"^/api/providers/(prov-[a-zA-Z0-9-]+)$")
    _RE_PROVIDER_LOGIN_PROBLEM = re.compile(r"^/api/providers/(prov-[a-zA-Z0-9-]+)/login-problem$")
    _RE_PROFILE = re.compile(r"^/api/agent-profiles/(agent-[a-zA-Z0-9-]+)$")
    _RE_PROJECT_SKILLS = re.compile(r"^/api/projects/(proj-[a-zA-Z0-9]+)/skills$")
    _RE_PROJECT_SKILL = re.compile(r"^/api/projects/(proj-[a-zA-Z0-9]+)/skills/(skill-[a-zA-Z0-9]+)$")
    _RE_PROJECT_SCHEDULES = re.compile(r"^/api/projects/(proj-[a-zA-Z0-9]+)/schedules$")
    _RE_SCHEDULE = re.compile(r"^/api/schedules/(sched-[a-zA-Z0-9]+)$")
    _RE_SCHEDULE_RUN = re.compile(r"^/api/schedules/(sched-[a-zA-Z0-9]+)/run$")
    _RE_SCHEDULE_RUNS = re.compile(r"^/api/schedules/(sched-[a-zA-Z0-9]+)/runs$")
    _RE_SCHEDULE_NOTIFICATIONS = re.compile(r"^/api/schedules/(sched-[a-zA-Z0-9]+)/notifications$")
    _RE_CONFIG_IMPORT = re.compile(r"^/api/config/imports/(cfgimp-[0-9a-f]{16})$")
    _RE_CONFIG_IMPORT_APPLY = re.compile(r"^/api/config/imports/(cfgimp-[0-9a-f]{16})/apply$")

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
        elif path == "/api/overview" and method == "GET":
            await self._handle_overview(writer)
        elif path == "/api/tasks" and method == "GET":
            await self._handle_list_tasks(query, writer)
        elif path == "/api/chat/send" and method == "POST":
            await self._handle_chat_send(body, writer)
        elif path == "/api/chat/targets" and method == "GET":
            await self._handle_chat_targets(writer)
        elif path == "/api/chat/messages" and method == "GET":
            await self._handle_chat_messages(query, writer)
        elif path == "/api/chat/messages" and method == "POST":
            await self._handle_chat_save_message(body, writer)
        elif path == "/api/chat/events" and method == "GET":
            await self._handle_sse_events(query, writer)
        elif path == "/api/tasks/create" and method == "POST":
            await self._answer(writer, HTTP_201, lambda: projects.create_task(projects.json_object(body)))
        elif path == "/api/tasks/cancel" and method == "POST":
            await self._handle_task_cancel(body, writer)
        elif path == "/api/tasks/stop" and method == "POST":
            await self._handle_task_stop(body, writer)
        elif path == "/api/tasks/start" and method == "POST":
            await self._handle_task_start(body, writer)
        elif path == "/api/tasks/delete" and method == "POST":
            await self._handle_task_delete(body, writer)
        elif (m := self._RE_TASK_ACTIVITY_STREAM.match(path)) and method == "GET":
            await self._handle_task_activity_stream(m.group(1), query, writer)
        elif (m := self._RE_TASK_ACTIVITY_SEQ.match(path)) and method == "GET":
            await self._handle_task_activity_detail(m.group(1), int(m.group(2)), writer)
        elif (m := self._RE_TASK_ACTIVITY.match(path)) and method == "GET":
            await self._handle_task_activity(m.group(1), query, writer)
        elif (m := self._RE_TASK_CONVERSATION.match(path)) and method == "GET":
            await self._handle_task_conversation(m.group(1), writer)
        elif (m := self._RE_TASK_CONFIG.match(path)) and method == "PUT":
            await self._handle_task_config_update(m.group(1), body, writer)
        elif (m := self._RE_TASK_PROJECT.match(path)) and method == "PUT":
            await self._answer(writer, HTTP_200, lambda: projects.move_task(
                m.group(1), projects.json_object(body), self._task_router))
        elif (m := self._RE_TASK_CONTINUE.match(path)) and method == "POST":
            await self._answer(writer, HTTP_200, lambda: self._continue_at_cap(m.group(1), body))
        elif (m := self._RE_TASK_START.match(path)) and method == "POST":
            await self._answer(writer, HTTP_200, lambda: projects.start_ready_task(
                m.group(1), self._supervisor.bus, self._web_adapter._address))
        elif (m := self._RE_TASK_DETAIL.match(path)) and method == "GET":
            await self._handle_task_detail(m.group(1), writer)
        elif path == "/api/projects" and method == "GET":
            await self._handle_list_projects(writer)
        elif path == "/api/projects" and method == "POST":
            await self._answer(writer, HTTP_201, lambda: projects.create_project(projects.json_object(body)))
        elif (m := self._RE_PROJECT.match(path)) and method == "PUT":
            await self._answer(writer, HTTP_200, lambda: projects.update_project(
                m.group(1), projects.json_object(body)))
        elif (m := self._RE_PROJECT.match(path)) and method == "DELETE":
            await self._answer(writer, HTTP_200, lambda: projects.delete_project(m.group(1)))
        elif (m := self._RE_PROJECT_SCHEDULES.match(path)) and method == "GET":
            await self._answer(writer, HTTP_200, lambda: schedules.list_schedules(m.group(1)))
        elif (m := self._RE_PROJECT_SCHEDULES.match(path)) and method == "POST":
            await self._answer(writer, HTTP_201, lambda: schedules.create_schedule(
                m.group(1), projects.json_object(body)))
        elif path == "/api/schedules/preview" and method == "POST":
            await self._answer(writer, HTTP_200, lambda: self._preview_schedule(body))
        elif (m := self._RE_SCHEDULE.match(path)) and method == "GET":
            await self._answer(writer, HTTP_200, lambda: schedules.get_schedule(m.group(1)))
        elif (m := self._RE_SCHEDULE.match(path)) and method == "PUT":
            await self._answer(writer, HTTP_200, lambda: schedules.update_schedule(
                m.group(1), projects.json_object(body)))
        elif (m := self._RE_SCHEDULE.match(path)) and method == "DELETE":
            await self._answer(writer, HTTP_200, lambda: schedules.delete_schedule(m.group(1)))
        elif (m := self._RE_SCHEDULE_RUN.match(path)) and method == "POST":
            await self._answer(writer, HTTP_200, lambda: self._run_schedule_now(m.group(1)))
        elif (m := self._RE_SCHEDULE_RUNS.match(path)) and method == "GET":
            await self._answer(writer, HTTP_200, lambda: schedules.list_runs(m.group(1), query))
        elif (m := self._RE_SCHEDULE_NOTIFICATIONS.match(path)) and method == "GET":
            await self._answer(writer, HTTP_200, lambda: schedules.list_notifications(m.group(1), query))
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
        elif (m := self._RE_PROJECT_INSTALLS.match(path)) and method == "GET":
            await self._handle_catalog(writer, HTTP_200, lambda: catalog.project_view(m.group(1)))
        elif (m := self._RE_PROJECT_SKILLS.match(path)) and method == "POST":
            await self._handle_catalog(writer, HTTP_201, lambda: project_skills.add_skill(
                m.group(1), projects.json_object(body)))
        elif (m := self._RE_PROJECT_SKILL.match(path)) and method == "GET":
            await self._handle_catalog(writer, HTTP_200, lambda: project_skills.get_skill(
                m.group(1), m.group(2)))
        elif (m := self._RE_PROJECT_SKILL.match(path)) and method == "PUT":
            await self._handle_catalog(writer, HTTP_200, lambda: project_skills.update_skill(
                m.group(1), m.group(2), projects.json_object(body)))
        elif (m := self._RE_PROJECT_SKILL.match(path)) and method == "DELETE":
            await self._handle_catalog(writer, HTTP_200, lambda: project_skills.delete_skill(
                m.group(1), m.group(2)))
        elif path == "/api/roles" and method == "GET":
            await self._handle_list_role_configs(writer)
        elif (m := self._RE_ROLE_CONFIG.match(path)) and method == "GET":
            await self._handle_get_role_config(m.group(1), writer)
        elif (m := self._RE_ROLE_CONFIG_RESET.match(path)) and method == "POST":
            await self._handle_reset_role_prompt(m.group(1), writer)
        elif (m := self._RE_ROLE_CONFIG.match(path)) and method == "PUT":
            await self._handle_update_role_config(m.group(1), body, writer)
        elif path == "/api/llm-records" and method == "GET":
            await self._handle_list_llm_records(query, writer)
        elif (m := self._RE_LLM_RECORD.match(path)) and method == "GET":
            await self._handle_get_llm_record(int(m.group(1)), writer)
        elif (m := self._RE_LLM_RECORD_EVENTS.match(path)) and method == "GET":
            await self._handle_llm_record_events(int(m.group(1)), query, writer)
        elif (m := self._RE_AGENT_USAGE.match(path)) and method == "GET":
            await self._handle_agent_usage(m.group(1), query, writer)
        elif path == "/api/registry" and method == "GET":
            await self._handle_registry(writer, HTTP_200, lambda: profile_registry.registry_view(
                server_claude=self._claude_bin))
        elif (m := self._RE_RUNTIME_PROBE.match(path)) and method == "POST":
            await self._handle_registry(writer, HTTP_200, lambda: profile_registry.probe_runtime(
                m.group(1), server_claude=self._claude_bin))
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
        elif path == "/api/agent-profiles" and method == "POST":
            await self._handle_registry(writer, HTTP_201, lambda: profile_registry.create_profile(
                projects.json_object(body)))
        elif (m := self._RE_PROFILE.match(path)) and method == "PUT":
            await self._handle_registry(writer, HTTP_200, lambda: profile_registry.update_profile(
                m.group(1), projects.json_object(body)))
        elif (m := self._RE_PROFILE.match(path)) and method == "DELETE":
            await self._handle_registry(writer, HTTP_200, lambda: profile_registry.delete_profile(m.group(1)))
        elif path == "/api/agent-settings" and method == "GET":
            await self._handle_agent_settings(writer, agent_settings.settings_view)
        elif path == "/api/agent-settings/system" and method == "PUT":
            await self._handle_agent_settings(writer, lambda: agent_settings.update_system(projects.json_object(body)))
        elif (m := self._RE_ROLE_SETTINGS.match(path)) and method == "PUT":
            await self._handle_agent_settings(writer, lambda: self._write_layer(
                body, lambda data: agent_settings.update_role(m.group(1), data)))
        elif (m := self._RE_PROJECT_AGENT_SETTINGS.match(path)) and method == "GET":
            await self._handle_agent_settings(writer, lambda: agent_settings.project_settings_view(m.group(1)))
        elif (m := self._RE_PROJECT_AGENT_SETTINGS.match(path)) and method == "PUT":
            await self._handle_agent_settings(writer, lambda: agent_settings.update_project(
                m.group(1), projects.json_object(body)))
        elif (m := self._RE_PROJECT_ROLE_SETTINGS.match(path)) and method == "PUT":
            await self._handle_agent_settings(writer, lambda: self._write_layer(
                body, lambda data: agent_settings.update_project(m.group(1), data, role=m.group(2))))
        elif path == "/api/config/export" and method == "GET":
            await self._handle_config_export(query, writer)
        elif (m := self._RE_CONFIG_IMPORT_APPLY.match(path)) and method == "POST":
            await self._handle_config_import(writer, HTTP_200, lambda imports: self._apply_import(imports, m.group(1)))
        elif (m := self._RE_CONFIG_IMPORT.match(path)) and method == "DELETE":
            await self._handle_config_import(writer, HTTP_200, lambda imports: imports.cancel(m.group(1)))
        elif path == "/api/catalog" and method == "GET":
            await self._handle_catalog(writer, HTTP_200, catalog.catalog_view)
        elif path == "/api/catalog/import" and method == "POST":
            await self._handle_catalog(writer, HTTP_200, library.import_from_machine)
        elif path == "/api/catalog/upload" and method == "POST":
            await self._handle_catalog(writer, HTTP_201, lambda: catalog_sources.upload(projects.json_object(body)))
        elif path == "/api/catalog/git/preview" and method == "POST":
            await self._handle_catalog(writer, HTTP_200, lambda: catalog_sources.git_preview(
                projects.json_object(body)))
        elif path == "/api/catalog/git/import" and method == "POST":
            await self._handle_catalog(writer, HTTP_200, lambda: catalog_sources.git_import(
                projects.json_object(body)))
        elif path == "/api/catalog/skills" and method == "POST":
            await self._handle_catalog(writer, HTTP_201, lambda: catalog_sources.create_skill(
                projects.json_object(body)))
        elif path == "/api/catalog/mcp" and method == "POST":
            await self._handle_catalog(writer, HTTP_201, lambda: mcp.create(projects.json_object(body)))
        elif path == "/api/catalog/mcp/import" and method == "POST":
            await self._handle_catalog(writer, HTTP_200, mcp.import_from_machine)
        elif (m := self._RE_CATALOG_ITEM.match(path)) and method == "GET":
            await self._handle_catalog(writer, HTTP_200, lambda: catalog.item_detail(m.group(1)))
        elif (m := self._RE_CATALOG_ITEM.match(path)) and method == "PUT":
            await self._handle_catalog(writer, HTTP_200, lambda: catalog.update_item(
                m.group(1), projects.json_object(body)))
        elif (m := self._RE_CATALOG_ITEM.match(path)) and method == "DELETE":
            await self._handle_catalog(writer, HTTP_200, lambda: catalog.delete_item(m.group(1)))
        elif path == "/api/installs" and method == "POST":
            await self._handle_catalog(writer, HTTP_201, lambda: catalog.install(projects.json_object(body)))
        elif (m := self._RE_INSTALL.match(path)) and method == "PUT":
            await self._handle_catalog(writer, HTTP_200, lambda: catalog.update_install(
                m.group(1), projects.json_object(body)))
        elif (m := self._RE_INSTALL.match(path)) and method == "DELETE":
            await self._handle_catalog(writer, HTTP_200, lambda: catalog.uninstall(m.group(1)))
        elif path == "/api/channels/lark/status" and method == "GET":
            await self._answer(writer, HTTP_200, self._lark_status)
        elif path == "/api/channels/lark/people" and method == "GET":
            await self._answer(writer, HTTP_200, lambda: self._lark_people(query.get("q", "")))
        elif path == "/api/channel-configs" and method == "GET":
            await self._handle_list_channel_configs(writer)
        elif (m := self._RE_CHANNEL_CONFIG.match(path)) and method == "GET":
            await self._handle_get_channel_config(m.group(1), writer)
        elif (m := self._RE_CHANNEL_CONFIG.match(path)) and method == "PUT":
            await self._handle_update_channel_config(m.group(1), body, writer)
        else:
            writer.write(_json_response(HTTP_404, {"error": "not found"}))
            await writer.drain()

    # --- API handlers ---

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

    async def _preview_schedule(self, body: bytes) -> dict[str, Any]:
        return schedules.preview(projects.json_object(body))

    async def _run_schedule_now(self, schedule_id: str) -> dict[str, Any]:
        if self._scheduler is None:
            raise projects.RequestError(HTTP_409, "the scheduler is not running in this server")
        return await schedules.run_now(schedule_id, self._scheduler)

    async def _handle_list_projects(self, writer: asyncio.StreamWriter) -> None:
        try:
            result = await projects.list_projects()
        except Exception:
            logger.exception("DB unavailable for list_projects")
            writer.write(_json_response(HTTP_500, {"error": "database unavailable"}))
            await writer.drain()
            return
        writer.write(_json_response(HTTP_200, _serialize(result)))
        await writer.drain()

    async def _checks_now(self) -> list[dict[str, str]]:
        """The startup self-check, with the Codex login read now: a run may have found it forked since."""
        if not any(check.get("id") == "codex_login" for check in self._self_checks):
            return self._self_checks
        codex = (await self_check.codex_login_check()).to_dict()
        return [codex if check.get("id") == "codex_login" else check for check in self._self_checks]

    async def _handle_status(self, writer: asyncio.StreamWriter) -> None:
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
            # Every task counted; "running" is every task in progress, "pending" the queued ones.
            "tasks": {
                "total": counts["total"],
                "running": counts["in_progress"],
                "pending": counts["queued"],
                "completed": counts["completed"],
            },
            "channels": {"web": web_health},
            "checks": await self._checks_now(),
        }
        writer.write(_json_response(HTTP_200, status))
        await writer.drain()

    async def _handle_overview(self, writer: asyncio.StreamWriter) -> None:
        try:
            result = await overview.build_overview(task_router=self._task_router, worker_pool=self._worker_pool,
                                                   manager=self._manager, supervisor=self._supervisor)
        except Exception:
            logger.exception("DB unavailable for the overview")
            writer.write(_json_response(HTTP_500, {"error": "database unavailable"}))
            await writer.drain()
            return
        writer.write(_json_response(HTTP_200, _serialize(result)))
        await writer.drain()

    async def _handle_list_tasks(self, query: dict[str, str], writer: asyncio.StreamWriter) -> None:
        try:
            group = TaskGroup(query["group"]) if query.get("group") else None
        except ValueError:
            groups = ", ".join(g.value for g in TaskGroup)
            writer.write(_json_response(HTTP_400, {"error": f"group must be one of {groups}"}))
            await writer.drain()
            return
        try:
            from ..db.llm_records import task_costs
            from ..db.tasks import list_tasks as db_list_tasks
            tasks = await db_list_tasks(limit=50, project_id=query.get("project_id") or None, group=group,
                                        schedule_id=query.get("schedule_id") or None)
            costs = await task_costs([t["id"] for t in tasks])
            result = []
            for t in tasks:
                result.append({
                    "task_id": t.get("id", ""),
                    "requirement": t.get("requirement", ""),
                    "goal_id": t.get("goal_id", ""),
                    "project_id": t.get("project_id", ""),
                    "project_name": t.get("project_name", ""),
                    "work_dir": t.get("work_dir", ""),
                    "status": t.get("status", "pending"),
                    "group": task_group(t.get("status")).value,
                    "phase": t.get("phase", "pending"),
                    "planner_id": t.get("planner_id"),
                    "correlation_id": t.get("correlation_id", ""),
                    "created_at": t.get("created_at"),
                    "completed_at": t.get("completed_at"),
                    # The schedule that created it (null for any other task, or once the schedule is deleted).
                    "schedule_id": t.get("schedule_id"),
                    "schedule_name": t.get("schedule_name"),
                    # What the task's Planner, Workers and Reviewers spent so far.
                    "cost": costs.get(t.get("id", "")),
                })
        except Exception:
            logger.exception("DB unavailable for list_tasks")
            writer.write(_json_response(HTTP_500, {"error": "database unavailable"}))
            await writer.drain()
            return
        writer.write(_json_response(HTTP_200, {"tasks": _serialize(result)}))
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

        try:
            target = resolve_chat_target(data.get("target"), self._task_router)
        except ChatTargetError as exc:
            writer.write(_json_response(exc.status, {"error": str(exc)}))
            await writer.drain()
            return

        session_id = data.get("session_id", "")
        message_id = data.get("message_id", "")

        if session_id and message_id:
            try:
                from ..db.chat import save_message
                await save_message(
                    session_id=session_id, message_id=message_id,
                    role="user", content=text,
                )
            except Exception:
                logger.debug("failed to persist user chat message", exc_info=True)

        result = await self._web_adapter.handle_user_message(
            text=text,
            session_id=session_id or data.get("session_id"),
            goal_id=data.get("goal_id"),
            language=data.get("language", "zh"),
            attachments=data.get("attachments"),
            target=target,
            message_id=message_id or None,
        )
        writer.write(_json_response(HTTP_200, result))
        await writer.drain()

    async def _handle_chat_targets(self, writer: asyncio.StreamWriter) -> None:
        """The Manager, then each recent task's Planner and step agents (finished ones too)."""
        targets = await list_chat_targets(self._task_router)
        writer.write(_json_response(HTTP_200, {"targets": _serialize(targets)}))
        await writer.drain()

    async def _handle_chat_messages(
        self, query: dict[str, str], writer: asyncio.StreamWriter,
    ) -> None:
        session_id = query.get("session_id", "")
        if not session_id:
            writer.write(_json_response(HTTP_400, {"error": "session_id is required"}))
            await writer.drain()
            return
        try:
            from ..db.chat import list_messages
            messages = await list_messages(session_id)
            writer.write(_json_response(HTTP_200, {"messages": _serialize(messages)}))
        except Exception:
            writer.write(_json_response(HTTP_200, {"messages": []}))
        await writer.drain()

    async def _handle_chat_save_message(
        self, body: bytes, writer: asyncio.StreamWriter,
    ) -> None:
        try:
            data = json.loads(body.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            writer.write(_json_response(HTTP_400, {"error": "invalid JSON"}))
            await writer.drain()
            return
        session_id = data.get("session_id", "")
        message_id = data.get("message_id", "")
        role = data.get("role", "")
        content = data.get("content", "")
        if not all([session_id, message_id, role]):
            writer.write(_json_response(HTTP_400, {"error": "session_id, message_id, role required"}))
            await writer.drain()
            return
        try:
            from ..db.chat import save_message
            await save_message(
                session_id=session_id, message_id=message_id,
                role=role, content=content,
            )
        except Exception:
            logger.debug("failed to save chat message", exc_info=True)
        writer.write(_json_response(HTTP_200, {"ok": True}))
        await writer.drain()

    async def _handle_sse_events(
        self,
        query: dict[str, str],
        writer: asyncio.StreamWriter,
    ) -> None:
        cid = query.get("cid", "")
        if not cid and query.get("session"):
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

    async def _handle_task_config_update(
        self, task_id: str, body: bytes, writer: asyncio.StreamWriter,
    ) -> None:
        try:
            data = json.loads(body.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            writer.write(_json_response(HTTP_400, {"error": "invalid JSON body"}))
            await writer.drain()
            return

        from ..db.tasks import update_task_config, get_task

        task = await get_task(task_id)
        if task is None:
            writer.write(_json_response(HTTP_404, {"error": "task not found"}))
            await writer.drain()
            return

        if task.get("status") != "init":
            writer.write(_json_response(HTTP_400, {"error": "can only edit config when status is init"}))
            await writer.drain()
            return

        config = data.get("config")
        if config is not None and not isinstance(config, dict):
            writer.write(_json_response(HTTP_400, {"error": "config must be a JSON object"}))
            await writer.drain()
            return

        work_dir = None
        if "work_dir" in data:
            from .tiers.task import resolve_work_dir
            try:
                work_dir = resolve_work_dir(data.get("work_dir"))
            except ValueError as exc:
                writer.write(_json_response(HTTP_400, {"error": str(exc)}))
                await writer.drain()
                return

        ok = await update_task_config(
            task_id,
            requirement=data.get("requirement"),
            work_dir=work_dir,
            config=config,
        )
        if not ok:
            writer.write(_json_response(HTTP_500, {"error": "update failed"}))
            await writer.drain()
            return

        updated = await get_task(task_id)
        result = _serialize({
            "task_id": updated["id"],
            "requirement": updated.get("requirement", ""),
            "work_dir": updated.get("work_dir", ""),
            "config": updated.get("config", {}),
            "status": updated.get("status", ""),
        })
        writer.write(_json_response(HTTP_200, result))
        await writer.drain()

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

    async def _handle_task_stop(
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
            writer.write(_json_response(HTTP_400, {"error": "task_id is required"}))
            await writer.drain()
            return

        from .bus import MessageBus
        from .message import TASK_ROUTER_ADDRESS, MessageEnvelope, MessageType

        bus = self._supervisor.bus
        envelope = MessageEnvelope.create(
            source=self._web_adapter._address,
            target=TASK_ROUTER_ADDRESS,
            message_type=MessageType.TASK_STOP,
            payload={"task_id": task_id},
        )
        await bus.publish(envelope)
        writer.write(_json_response(HTTP_200, {"stopped": task_id}))
        await writer.drain()

    async def _handle_task_start(
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
            writer.write(_json_response(HTTP_400, {"error": "task_id is required"}))
            await writer.drain()
            return

        from ..db.tasks import get_task
        from .message import TASK_ROUTER_ADDRESS, MessageEnvelope, MessageType

        task = await get_task(task_id)
        if task is None:
            writer.write(_json_response(HTTP_404, {"error": "task not found"}))
            await writer.drain()
            return
        if not (task.get("work_dir") or "").strip():
            # There is no default directory; a task without one cannot run.
            writer.write(_json_response(HTTP_400, {"error": "set the task's work directory before starting it"}))
            await writer.drain()
            return

        bus = self._supervisor.bus
        envelope = MessageEnvelope.create(
            source=self._web_adapter._address,
            target=TASK_ROUTER_ADDRESS,
            message_type=MessageType.TASK_RESTART,
            payload={"task_id": task_id},
        )
        await bus.publish(envelope)
        writer.write(_json_response(HTTP_200, {"started": task_id}))
        await writer.drain()

    async def _continue_at_cap(self, task_id: str, body: bytes) -> dict[str, Any]:
        """Raise the cap of a task that failed at it, then restart it from its saved plan."""
        from .message import TASK_ROUTER_ADDRESS, MessageEnvelope, MessageType

        result = await projects.raise_cap_to_continue(task_id, projects.json_object(body))
        await self._supervisor.bus.publish(MessageEnvelope.create(
            source=self._web_adapter._address,
            target=TASK_ROUTER_ADDRESS,
            message_type=MessageType.TASK_RESTART,
            payload={"task_id": task_id, "resume": True},
        ))
        return {**result, "continued": task_id}

    async def _handle_task_delete(
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
            writer.write(_json_response(HTTP_400, {"error": "task_id is required"}))
            await writer.drain()
            return

        from ..db.tasks import ACTIVE_STATUSES, delete_task, get_task

        task = await get_task(task_id)
        if task is None:
            writer.write(_json_response(HTTP_404, {"error": "task not found"}))
            await writer.drain()
            return

        if task.get("status", "") in ACTIVE_STATUSES:
            writer.write(
                _json_response(HTTP_400, {"error": "stop the task before deleting"})
            )
            await writer.drain()
            return

        await delete_task(task_id)
        writer.write(_json_response(HTTP_200, {"deleted": task_id}))
        await writer.drain()

    # --- Task detail handlers ---

    async def _handle_task_detail(
        self, task_id: str, writer: asyncio.StreamWriter,
    ) -> None:
        try:
            from ..db.agent_setting_layers import profile_overrides
            from ..db.llm_records import task_costs
            from ..db.tasks import get_task_with_steps
            raw = await get_task_with_steps(task_id)
            cost = (await task_costs([task_id]))[task_id] if raw else None
            overrides = await profile_overrides(task_id) if raw else {}
        except Exception:
            logger.exception("DB unavailable for task_detail %s", task_id)
            writer.write(_json_response(HTTP_500, {"error": "database unavailable"}))
            await writer.drain()
            return

        if not raw:
            writer.write(_json_response(HTTP_404, {"error": "task not found"}))
            await writer.drain()
            return

        result = {
            "task_id": raw.get("id", ""),
            "requirement": raw.get("requirement", ""),
            "goal_id": raw.get("goal_id", ""),
            "project_id": raw.get("project_id", ""),
            "project_name": raw.get("project_name", ""),
            "status": raw.get("status", "pending"),
            "group": task_group(raw.get("status")).value,
            "phase": raw.get("phase", "pending"),
            "planner_id": raw.get("planner_id"),
            "correlation_id": raw.get("correlation_id", ""),
            "work_dir": raw.get("work_dir", ""),
            "config": raw.get("config", {}),
            # The Agent Profile the task chose for some of its roles, over the layers (agent_settings.Layer.TASK).
            "profile_overrides": {role: profile_registry.profile_view(profile) for role, profile in overrides.items()},
            "origin_channel_id": raw.get("origin_channel_id", ""),
            "result_summary": raw.get("result_summary"),
            "error": raw.get("error"),
            "fail_kind": raw.get("fail_kind"),  # "task_budget": it failed at its project's task spend cap
            # The task's spend cap as the Planner enforces it (its own, else its project's); null for none.
            "task_budget_usd": ProjectExecution.from_row(
                {"id": raw.get("project_id"), "task_budget_usd": raw.get("task_budget_usd")}).task_budget_usd,
            # The task's own cap (a scheduled run's), raised alone when it continues at its cap; null for none.
            "own_budget_usd": raw.get("budget_usd"),
            "schedule_id": raw.get("schedule_id"),
            "schedule_name": raw.get("schedule_name"),
            "created_at": raw.get("created_at"),
            "updated_at": raw.get("updated_at"),
            "completed_at": raw.get("completed_at"),
            "steps": raw.get("steps", []),
            # The task journal, oldest first: an entry per accepted step with its summary and learnings.
            "journal": raw.get("journal", []),
            # The Planner's progress notes (journal kind progress), newest first: done, now, next, risks, estimate, at.
            "progress": progress_notes(raw.get("journal", [])),
            # What the task's Planner, Workers and Reviewers spent: the total, by role, and the run count.
            "cost": cost,
        }
        writer.write(_json_response(HTTP_200, _serialize(result)))
        await writer.drain()

    async def _handle_task_activity(
        self, task_id: str, query: dict[str, str], writer: asyncio.StreamWriter,
    ) -> None:
        since = int(query.get("since", "0"))
        limit = min(int(query.get("limit", "100")), 500)
        try:
            from ..db.activity import list_activity
            rows = await list_activity(task_id, since_seq=since, limit=limit)
        except Exception:
            rows = []
        writer.write(_json_response(HTTP_200, {"activity": [_serialize(r) for r in rows]}))
        await writer.drain()

    async def _handle_task_activity_detail(
        self, task_id: str, seq: int, writer: asyncio.StreamWriter,
    ) -> None:
        try:
            from ..db.activity import get_activity_detail
            row = await get_activity_detail(seq)
        except Exception:
            row = None
        if row is None:
            writer.write(_json_response(HTTP_404, {"error": "activity not found"}))
            await writer.drain()
            return
        writer.write(_json_response(HTTP_200, _serialize(row)))
        await writer.drain()

    async def _handle_task_activity_stream(
        self,
        task_id: str,
        query: dict[str, str],
        writer: asyncio.StreamWriter,
    ) -> None:
        """SSE stream for task activity with cursor-based gap replay."""
        since = int(query.get("since", "0"))

        sse_headers = (
            "HTTP/1.1 200 OK\r\n"
            "Content-Type: text/event-stream\r\n"
            "Cache-Control: no-cache\r\n"
            "Connection: keep-alive\r\n"
            "X-Accel-Buffering: no\r\n"
            "\r\n"
        )
        writer.write(sse_headers.encode("utf-8"))
        await writer.drain()

        cursor = since
        try:
            from ..db.activity import list_activity
            while True:
                rows = await list_activity(task_id, since_seq=cursor, limit=500)
                if not rows:
                    break
                for row in rows:
                    data = json.dumps(_serialize(row), ensure_ascii=False)
                    seq = row.get("seq", 0)
                    writer.write(f"id: {seq}\nevent: activity\ndata: {data}\n\n".encode("utf-8"))
                    await writer.drain()
                    cursor = seq
        except Exception:
            pass

        sub = self._broadcaster.subscribe(task_id)
        try:
            while True:
                try:
                    event = await asyncio.wait_for(sub.get(), timeout=20.0)
                except asyncio.TimeoutError:
                    writer.write(b": ping\n\n")
                    await writer.drain()
                    continue

                if event is None:
                    writer.write(b"event: done\ndata: {}\n\n")
                    await writer.drain()
                    break

                data = json.dumps(event, ensure_ascii=False)
                seq = event.get("seq", "")
                line = f"id: {seq}\nevent: activity\ndata: {data}\n\n"
                writer.write(line.encode("utf-8"))
                await writer.drain()

        except (ConnectionError, BrokenPipeError, asyncio.CancelledError):
            pass
        finally:
            self._broadcaster.unsubscribe(task_id, sub)

    async def _handle_task_conversation(
        self, task_id: str, writer: asyncio.StreamWriter,
    ) -> None:
        try:
            from ..db.activity import list_conversation
            rows = await list_conversation(task_id)
        except Exception:
            rows = []
        writer.write(_json_response(HTTP_200, {"conversation": [_serialize(r) for r in rows]}))
        await writer.drain()

    # --- Role config handlers ---

    async def _handle_list_role_configs(self, writer: asyncio.StreamWriter) -> None:
        try:
            from ..db.role_configs import list_configs
            configs = await list_configs()
        except Exception:
            configs = []
        writer.write(_json_response(HTTP_200, {"configs": configs}))
        await writer.drain()

    async def _handle_get_role_config(
        self, role: str, writer: asyncio.StreamWriter,
    ) -> None:
        try:
            from ..db.role_configs import get_config
            config = await get_config(role)
        except Exception:
            config = None
        if config is None:
            writer.write(_json_response(HTTP_404, {"error": f"role '{role}' not found"}))
        else:
            writer.write(_json_response(HTTP_200, config))
        await writer.drain()

    async def _handle_update_role_config(
        self, role: str, body: bytes, writer: asyncio.StreamWriter,
    ) -> None:
        try:
            data = json.loads(body.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            writer.write(_json_response(HTTP_400, {"error": "invalid JSON body"}))
            await writer.drain()
            return

        if not isinstance(data, dict):
            writer.write(_json_response(HTTP_400, {"error": "body must be a JSON object"}))
            await writer.drain()
            return

        try:
            data = agent_settings.role_config_fields(role, data)
        except agent_settings.SettingsError as exc:
            writer.write(_json_response(HTTP_400, {"error": str(exc)}))
            await writer.drain()
            return

        try:
            from ..db.role_configs import set_config
            updated = await set_config(role, data)
        except Exception as exc:
            writer.write(_json_response(HTTP_500, {"error": str(exc)}))
            await writer.drain()
            return

        if updated is None:
            writer.write(_json_response(HTTP_404, {"error": f"role '{role}' not found"}))
        else:
            writer.write(_json_response(HTTP_200, updated))
        await writer.drain()

    async def _handle_reset_role_prompt(
        self, role: str, writer: asyncio.StreamWriter,
    ) -> None:
        """Reset a role's prompt to the system-recommended default."""
        try:
            from ..db.role_configs import reset_prompt
            updated = await reset_prompt(role)
        except Exception as exc:
            writer.write(_json_response(HTTP_500, {"error": str(exc)}))
            await writer.drain()
            return

        if updated is None:
            writer.write(_json_response(
                HTTP_404, {"error": f"role '{role}' has no recommended prompt to reset to"},
            ))
        else:
            writer.write(_json_response(HTTP_200, updated))
        await writer.drain()

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

    async def _apply_import(self, imports: config_imports.ConfigImports, import_id: str) -> Any:
        """Apply an import; its layers may raise a concurrency limit."""
        result = await imports.apply(import_id)
        await self._recheck_concurrency()
        return result

    async def _recheck_concurrency(self) -> None:
        """Start the tasks and runs that wait for room a raised limit now gives; a lowered limit stops nothing."""
        try:
            await self._task_router.recheck()
            if self._worker_pool is not None:
                await self._worker_pool.recheck()
        except Exception:
            # The write stands; what waits starts when a task or run ends.
            logger.exception("re-checking the concurrency limits failed")

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

    # --- The capability catalog: its items, their installs and the skills projects write ---

    async def _handle_catalog(
        self, writer: asyncio.StreamWriter, status: str, request: Callable[[], Awaitable[Any]],
    ) -> None:
        """Answer with ``request()``'s result; a refused request is 400, an unknown item, install, project or
        skill 404, and an install that is there already or a name that is taken 409."""
        try:
            result = await request()
        except (catalog.CatalogError, library.LibraryError, project_skills.ProjectSkillError,
                projects.RequestError) as exc:
            status, result = getattr(exc, "status", HTTP_400), {"error": str(exc)}
        except (catalog.CatalogNotFound, project_skills.ProjectSkillNotFound) as exc:
            status, result = HTTP_404, {"error": str(exc)}
        except (catalog.CatalogConflict, project_skills.ProjectSkillConflict) as exc:
            status, result = HTTP_409, {"error": str(exc)}
        except Exception:
            logger.exception("catalog request failed")
            status, result = HTTP_500, {"error": "the catalog request failed; see the server log"}
        writer.write(_json_response(status, _serialize(result)))
        await writer.drain()

    # --- LLM record handlers ---

    async def _handle_get_llm_record(self, record_id: int, writer: asyncio.StreamWriter) -> None:
        from ..db.llm_records import get_record
        try:
            record = await get_record(record_id)
        except Exception:
            writer.write(_json_response(HTTP_500, {"error": "database unavailable"}))
            await writer.drain()
            return
        if record is None:
            writer.write(_json_response(HTTP_404, {"error": f"llm record {record_id} not found"}))
        else:
            writer.write(_json_response(HTTP_200, {"record": record}))
        await writer.drain()

    async def _handle_llm_record_events(
        self, record_id: int, query: dict[str, str], writer: asyncio.StreamWriter,
    ) -> None:
        """Transcript events after ``after`` (a seq), so a viewer can follow a running agent."""
        from ..db.llm_records import list_events
        try:
            after = int(query.get("after", "0"))
            limit = min(int(query.get("limit", "1000")), 5000)
        except ValueError:
            writer.write(_json_response(HTTP_400, {"error": "after and limit must be integers"}))
            await writer.drain()
            return
        try:
            events = await list_events(record_id, after_seq=after, limit=limit)
        except Exception:
            writer.write(_json_response(HTTP_500, {"error": "database unavailable"}))
            await writer.drain()
            return
        writer.write(_json_response(HTTP_200, {"events": events}))
        await writer.drain()

    async def _handle_agent_usage(
        self, agent_id: str, query: dict[str, str], writer: asyncio.StreamWriter,
    ) -> None:
        """An agent's spending (in total, by session, by UTC day) and its context over its latest turns."""
        from ..db.llm_records import agent_usage
        try:
            turns = min(max(int(query.get("turns", "100")), 1), 1000)
        except ValueError:
            writer.write(_json_response(HTTP_400, {"error": "turns must be an integer"}))
            await writer.drain()
            return
        try:
            usage = await agent_usage(agent_id, turns=turns)
        except Exception:
            logger.exception("DB unavailable for agent usage %s", agent_id)
            writer.write(_json_response(HTTP_500, {"error": "database unavailable"}))
            await writer.drain()
            return
        writer.write(_json_response(HTTP_200, _serialize(usage)))
        await writer.drain()

    async def _handle_list_llm_records(
        self, query: dict[str, str], writer: asyncio.StreamWriter,
    ) -> None:
        agent_id = query.get("agent_id")
        task_id = query.get("task_id")
        role = query.get("role")
        limit = min(int(query.get("limit", "100")), 500)
        offset = int(query.get("offset", "0"))
        try:
            from ..db.llm_records import list_records
            records = await list_records(
                agent_id=agent_id, task_id=task_id, role=role, limit=limit, offset=offset,
            )
        except Exception:
            records = []
        writer.write(_json_response(HTTP_200, {"records": records}))
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

    # --- Channel config handlers ---

    async def _lark_status(self) -> dict[str, Any]:
        from .channels import lark_settings

        return await lark_settings.status()

    async def _lark_people(self, q: str) -> dict[str, Any]:
        from .channels import lark_cli, lark_settings

        try:
            return await lark_settings.people(q)
        except ValueError as exc:
            raise projects.RequestError(HTTP_400, str(exc)) from None
        except lark_cli.LarkCliError as exc:
            raise projects.RequestError(HTTP_502, f"lark-cli could not search: {exc}") from None

    async def _handle_list_channel_configs(self, writer: asyncio.StreamWriter) -> None:
        from .channels.allowlist import channel_view

        try:
            from ..db.channel_configs import list_configs
            configs = await list_configs()
        except Exception:
            configs = []
        writer.write(_json_response(HTTP_200, {"configs": [channel_view(c) for c in configs]}))
        await writer.drain()

    async def _handle_get_channel_config(
        self, channel_id: str, writer: asyncio.StreamWriter,
    ) -> None:
        try:
            from ..db.channel_configs import get_config
            config = await get_config(channel_id)
        except Exception:
            config = None
        if config is None:
            writer.write(_json_response(HTTP_404, {"error": f"channel '{channel_id}' not found"}))
        else:
            from .channels.allowlist import channel_view
            writer.write(_json_response(HTTP_200, channel_view(config)))
        await writer.drain()

    async def _handle_update_channel_config(
        self, channel_id: str, body: bytes, writer: asyncio.StreamWriter,
    ) -> None:
        try:
            data = json.loads(body.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            writer.write(_json_response(HTTP_400, {"error": "invalid JSON body"}))
            await writer.drain()
            return

        if not isinstance(data, dict):
            writer.write(_json_response(HTTP_400, {"error": "body must be a JSON object"}))
            await writer.drain()
            return

        from .channels.allowlist import AllowlistError, channel_view, normalized_config
        from .profile_registry import SECRET_MASK

        # The bot token is a secret kept out of config: SECRET_MASK (or absent) keeps the
        # stored token, "" / null removes it, anything else stores it. It is never returned.
        token = data.pop("bot_token", SECRET_MASK)
        if token != SECRET_MASK and token is not None and (
            not isinstance(token, str) or len(token) > 4096
        ):
            writer.write(_json_response(HTTP_400, {"error": "bot_token must be a string (<=4096 chars) or null"}))
            await writer.drain()
            return

        try:
            from ..db.channel_configs import get_config, set_bot_token, set_config
            from ..db.connection import get_pool
            current = await get_config(channel_id)
            if current is None:
                writer.write(_json_response(HTTP_404, {"error": f"channel '{channel_id}' not found"}))
                await writer.drain()
                return
            if "config" in data:
                # The sender allowlist is validated before anything is written: a rejected
                # request (AllowlistError -> 400) must not change the stored token or config.
                kind = str(current.get("channel_type") or "")
                config = normalized_config(kind, data["config"])
                if kind == "lark":
                    from .channels import lark_settings
                    try:
                        config = await lark_settings.bound_config(current.get("config") or {}, config)
                    except ValueError as exc:
                        raise AllowlistError(str(exc)) from None
                data = {**data, "config": config}
            # Apply the token and the config in one transaction so a failure leaves neither changed.
            async with get_pool().acquire() as conn, conn.transaction():
                if token != SECRET_MASK:
                    await set_bot_token(channel_id, None if token in (None, "") else token, pool=conn)
                updated = await set_config(channel_id, data, pool=conn)
        except AllowlistError as exc:
            writer.write(_json_response(HTTP_400, {"error": str(exc)}))
            await writer.drain()
            return
        except Exception as exc:
            writer.write(_json_response(HTTP_500, {"error": str(exc)}))
            await writer.drain()
            return

        if updated is None:
            writer.write(_json_response(HTTP_404, {"error": f"channel '{channel_id}' not found"}))
        else:
            writer.write(_json_response(HTTP_200, channel_view(updated)))
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
            f"\r\n"
        )
        writer.write(headers.encode("utf-8") + data)
        await writer.drain()
