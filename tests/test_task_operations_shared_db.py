"""The console and Manager share task controls and durable Planner supplements."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from looper.agents.bus import MessageBus
from looper.agents.channels.web import WebChannelAdapter
from looper.agents.http_server import AgentHTTPServer
from looper.agents.message import MessageType
from looper.agents.ops import Call, Turn
from looper.agents.ops.tasks import _instruct
from looper.agents.ops.clients import ClientOps
from looper.agents.ops.service import Platform
from looper.db import projects, task_changes, tasks
from tests.db_support import run_db


class _Writer:
    def __init__(self) -> None:
        self.data = b""

    def write(self, data: bytes) -> None:
        self.data += data

    async def drain(self) -> None:
        pass


def _server(tmp_path: Path) -> tuple[AgentHTTPServer, MessageBus, Any]:
    bus = MessageBus(runtime_root=tmp_path)
    adapter = WebChannelAdapter()
    adapter._bus = bus
    router = SimpleNamespace(has_planner=lambda task_id: task_id == "task-a")
    server = AgentHTTPServer(web_adapter=adapter, task_router=router, supervisor=SimpleNamespace(bus=bus),
                             runtime_root=tmp_path, client_ops=ClientOps(Platform(bus=bus, task_router=router)))
    return server, bus, router


async def _post(server: AgentHTTPServer, path: str, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    writer = _Writer()
    if path.startswith("/api/v1/"):
        from looper.agents.api_v1 import OPERATOR

        await server._handle_v1("POST", path, {}, {}, json.dumps(body).encode(), writer, OPERATOR)
    else:
        await server._route_api("POST", path, {}, json.dumps(body).encode(), writer)
    head, _, payload = writer.data.partition(b"\r\n\r\n")
    answer = json.loads(payload)
    if path.startswith("/api/v1/"):
        answer = answer["result"] if answer["status"] == "ok" else {"error": answer["error"]["message"]}
    return int(head.split()[1]), answer


@pytest.mark.parametrize(("path", "status", "extra", "allowed", "message_type", "message_extra"), [
    ("stop", "completed", {}, False, MessageType.TASK_STOP, {}),
    ("stop", "stopped", {}, False, MessageType.TASK_STOP, {}),
    ("stop", "executing", {}, True, MessageType.TASK_STOP, {}),
    ("cancel", "init", {}, False, MessageType.TASK_CANCEL, {}),
    ("cancel", "cancelled", {}, False, MessageType.TASK_CANCEL, {}),
    ("cancel", "executing", {}, True, MessageType.TASK_CANCEL, {}),
    ("restart", "completed", {}, False, MessageType.TASK_RESTART, {}),
    ("start", "init", {}, True, MessageType.TASK_START, {}),
    ("restart", "executing", {}, False, MessageType.TASK_RESTART, {}),
    ("start_over", "executing", {}, False, MessageType.TASK_RESTART, {}),
    ("restart", "stopped", {}, True, MessageType.TASK_RESTART, {"mode": "continue"}),
    ("start_over", "failed", {}, True, MessageType.TASK_RESTART, {"mode": "start_over"}),
])
def test_console_controls_check_state_before_sending(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, path: str, status: str, extra: dict[str, Any],
    allowed: bool, message_type: MessageType, message_extra: dict[str, Any],
) -> None:
    async def test(pool: Any) -> None:
        project = await projects.create_project(name="calc", work_dir=str(tmp_path), pool=pool)
        await tasks.create_task(task_id="task-a", requirement="Build calc", status=status,
                                project_id=project["id"], work_dir=str(tmp_path), pool=pool)
        server, bus, _ = _server(tmp_path)
        response, body = await _post(server, f"/api/v1/ops/task_control/{path}", {"task_id": "task-a", **extra})
        queue = bus.get_queue("task-router")
        if allowed:
            assert response == 200, body
            assert body["task_id"] == "task-a"
            assert body[{"stop": "stopped", "cancel": "cancelled", "start": "started",
                         "restart": "restarted", "start_over": "started_over"}[path]] is True
            message = queue.get_nowait()
            assert (message.message_type, message.payload) == (message_type, {"task_id": "task-a", **message_extra})
        else:
            assert response == 409, body
            assert status in body["error"]
        assert queue.empty()
        assert (await tasks.get_task("task-a", pool=pool))["status"] == status

    run_db(test, monkeypatch, timeout=20)


@pytest.mark.parametrize(("exists", "work_dir", "extra", "expected"), [
    (False, "work", {}, 404),
    (True, "work", {"start_over": "yes"}, 400),
])
def test_start_rejects_invalid_requests_without_sending(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, exists: bool, work_dir: str,
    extra: dict[str, Any], expected: int,
) -> None:
    async def test(pool: Any) -> None:
        if exists:
            project = await projects.create_project(name="calc", pool=pool)
            await tasks.create_task(task_id="task-a", requirement="Build calc", status="init",
                                    project_id=project["id"], work_dir=str(tmp_path) if work_dir else "", pool=pool)
        server, bus, _ = _server(tmp_path)
        status, body = await _post(server, "/api/v1/ops/task_control/start", {"task_id": "task-a", **extra})
        assert status == expected, body
        assert body["error"]
        assert bus.get_queue("task-router").empty()
        if exists:
            assert (await tasks.get_task("task-a", pool=pool))["status"] == "init"

    run_db(test, monkeypatch, timeout=20)


@pytest.mark.parametrize("entry", ["console", "manager"])
def test_supplement_storage_failure_keeps_each_entrys_delivery_contract(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, entry: str,
) -> None:
    async def fail(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("supplement storage unavailable")

    async def test(pool: Any) -> None:
        project = await projects.create_project(name="calc", work_dir=str(tmp_path), pool=pool)
        await tasks.create_task(task_id="task-a", requirement="Build calc", status="executing",
                                project_id=project["id"], work_dir=str(tmp_path), pool=pool)
        server, bus, router = _server(tmp_path)
        monkeypatch.setattr(task_changes, "add_supplement", fail)
        queue = bus.get_queue("planner-task-a")
        if entry == "console":
            status, result = await _post(server, "/api/chat/send", {
                "text": "Use CSV", "target": {"kind": "planner", "task_id": "task-a"}})
            assert status == 200, result
            message = queue.get_nowait()
            assert message.message_type is MessageType.USER_MESSAGE
            assert message.payload["text"] == "Use CSV"
            assert "supplement_id" not in message.payload
        else:
            with pytest.raises(RuntimeError, match="supplement storage unavailable"):
                await _instruct(Call(
                    arguments={"task_id": "task-a", "text": "Use CSV"},
                    turn=Turn(channel="lark", chat="chat-1", message_id="message-1"),
                    platform=SimpleNamespace(bus=bus, task_router=router)))
        assert queue.empty()
        assert (await task_changes.pending("task-a", pool=pool)).empty

    run_db(test, monkeypatch, timeout=20)


@pytest.mark.parametrize("entry", ["console", "manager"])
def test_each_supplement_entry_stores_once_and_wakes_once(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, entry: str,
) -> None:
    async def test(pool: Any) -> None:
        project = await projects.create_project(name="calc", work_dir=str(tmp_path), pool=pool)
        await tasks.create_task(task_id="task-a", requirement="Build calc", status="executing",
                                project_id=project["id"], work_dir=str(tmp_path), pool=pool)
        server, bus, router = _server(tmp_path)
        if entry == "console":
            status, result = await _post(server, "/api/chat/send", {
                "text": "Use CSV", "target": {"kind": "planner", "task_id": "task-a"},
                "session_id": "task-thread", "message_id": "message-1"})
            assert status == 200, result
        else:
            result = await _instruct(Call(
                arguments={"task_id": "task-a", "text": "Use CSV"},
                turn=Turn(channel="lark", chat="chat-1", message_id="message-1"),
                platform=SimpleNamespace(bus=bus, task_router=router)))
        pending = await task_changes.pending("task-a", pool=pool)
        assert len(pending.supplements) == 1
        supplement = pending.supplements[0]
        assert (supplement.text, supplement.via) == ("Use CSV", "chat" if entry == "console" else "manager:lark")
        assert await pool.fetchval("SELECT count(*) FROM task_journal WHERE task_id = 'task-a' AND kind = 'supplement'") == 1
        queue = bus.get_queue("planner-task-a")
        message = queue.get_nowait()
        assert queue.empty()
        if entry == "console":
            assert message.message_type is MessageType.USER_MESSAGE
            assert message.payload["supplement_id"] == supplement.id
            assert message.payload["text"] == "Use CSV"
            assert message.payload["channel_context"]["session_id"] == "task-thread"
            assert result["correlation_id"] == message.correlation_id
            assert result["events_url"] == f"/api/chat/events?cid={message.correlation_id}"
        else:
            assert message.message_type is MessageType.TASK_CHANGE
            assert message.payload == {"task_id": "task-a"}
            assert result["supplement_id"] == supplement.id

    run_db(test, monkeypatch, timeout=20)
