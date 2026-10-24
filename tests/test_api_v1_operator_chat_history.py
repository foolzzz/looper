"""Typed operator target projections, request boundaries, retired routes and generated CLI."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from looper import loopercli
from looper.agents import api_v1, chat_targets
from looper.agents.ops.clients import ClientOps
from looper.db import chat
from tests.test_api_v1_auth import CLIENT_TOKEN, _bearer, _send, _serving, clients
from tests.test_loopercli import Server, _send as cli_send, write_client_file
from tests.test_operator_auth import TOKEN


@pytest.fixture
def history(monkeypatch):
    monkeypatch.setattr(chat_targets.db_tasks, "list_tasks", AsyncMock(return_value=[{
        "id": "task-one", "name": "Example task", "requirement": "Check changes", "status": "running",
        "created_at": "2026-10-24"}]))
    monkeypatch.setattr(chat_targets.db_steps, "list_steps_for_tasks", AsyncMock(return_value=[]))
    monkeypatch.setattr(chat_targets.llm_records, "list_task_runs", AsyncMock(return_value=[{
        "task_id": "task-one", "agent_id": kind + "-one", "status": "completed", "created_at": "2026-10-24",
        "task_role": {"id": "tester", "kind": "work", "version": 1} if kind == "worker" else None
    } for kind in ("worker", "reviewer")]))
    monkeypatch.setattr(chat, "thread_owner", AsyncMock(return_value="manager"))
    monkeypatch.setattr(chat, "list_messages", AsyncMock(return_value=[{
        "message_id": "one", "role": "assistant", "content": "Saved reply", "created_at": "2026-10-24",
        "channel": "lark", "delivery_failure": "send_failed", "delivery_error": "offline"}]))
    monkeypatch.setattr(chat, "list_sessions", AsyncMock(return_value=[{
        "session_id": "old-chat", "channel": "web", "first_message_at": "2026-10-24",
        "last_message_at": "2026-10-24", "last_role": "assistant", "last_content": "Saved reply", "message_count": 1}]))
    monkeypatch.setattr(chat, "save_message", AsyncMock())


def prepare(server):
    server._task_router.running_planners = lambda: [{"task_id": "task-one", "planner_id": "planner-task-one",
        "name": "Example task", "requirement": "Check changes", "status": "running"}]
    server._client_ops = ClientOps(SimpleNamespace())


CASES = [("targets", "GET", "targets", {}), ("sessions", "GET", "sessions", {}),
         ("messages", "GET", "messages", {"session_id": "old-chat"}),
         ("save_message", "POST", "messages", {"session_id": "old-chat", "message_id": "one",
                                                  "role": "user", "content": "hello"})]


@pytest.mark.parametrize("action,method,legacy,body", CASES)
def test_typed_positive_control_and_authenticated_legacy_404(tmp_path, history, action, method, legacy, body):
    async def run():
        async with _serving(tmp_path) as server:
            prepare(server)
            path = f"/api/v1/operator_chat/{action}"
            status, _, answer = await _send(server, f"POST {path}", _bearer(TOKEN), json.dumps(body))
            operation = api_v1.find("POST", path)
            assert status == 200 and answer["result"]
            api_v1.validate_arguments(operation.result, answer["result"])
            assert operation.operator_only and not operation.hand and operation.tool is None
            assert (await _send(server, f"{method} /api/chat/{legacy}", _bearer(TOKEN), json.dumps(body)))[0] == 404
    asyncio.run(run())


def test_target_union_retains_writable_managers_and_planners_and_read_only_step_agents(tmp_path, history):
    async def run():
        async with _serving(tmp_path) as server:
            prepare(server)
            status, _, answer = await _send(server, "POST /api/v1/operator_chat/targets", _bearer(TOKEN), "{}")
            assert status == 200
            api_v1.validate_arguments(api_v1.find("POST", "/api/v1/operator_chat/targets").result, answer["result"])
            manager, planner, worker, reviewer = answer["result"]["targets"]
            assert manager == {"id": "manager", "kind": "manager", "can_send": True}
            assert planner["can_send"] is True and planner["requirement"] == "Check changes"
            assert worker["task_role"] == {"id": "tester", "kind": "work", "version": 1}
            assert reviewer["step_index"] is None and "task_role" not in reviewer
            assert all(not row["can_send"] and row["instruction"] == "" for row in (worker, reviewer))
    asyncio.run(run())


@pytest.mark.parametrize("action,body", [("targets", {"extra": True}), ("sessions", {"manager": None}),
    ("sessions", {"session_id": 1}), ("messages", {}), ("messages", {"session_id": ""}),
    ("messages", {"session_id": "old-chat", "manager": None}),
    ("save_message", {"session_id": "old-chat", "message_id": "one", "role": 1, "content": "hello"}),
    ("save_message", {"session_id": "old-chat", "message_id": "one", "role": "user"})])
def test_requests_are_closed_and_identifiers_are_typed(tmp_path, history, action, body):
    async def run():
        async with _serving(tmp_path) as server:
            prepare(server)
            status, _, answer = await _send(server, f"POST /api/v1/operator_chat/{action}", _bearer(TOKEN), json.dumps(body))
            assert status == 400 and answer["error"]["code"] == "invalid"
    asyncio.run(run())


@pytest.mark.parametrize("action", [case[0] for case in CASES])
def test_client_is_refused_before_body_validation(tmp_path, clients, action):
    async def run():
        async with _serving(tmp_path) as server:
            status, _, answer = await _send(server, f"POST /api/v1/operator_chat/{action}", _bearer(CLIENT_TOKEN), "invalid json")
            assert status == 403 and answer["error"]["code"] == "forbidden"
    asyncio.run(run())


@pytest.mark.parametrize("action,args,body", [
    ("targets", [], {}), ("sessions", ["--manager", "manager", "--session-id", "old-chat"],
     {"manager": "manager", "session_id": "old-chat"}),
    ("messages", ["--session-id", "old-chat"], {"session_id": "old-chat"}),
    ("save_message", ["--session-id", "old-chat", "--message-id", "one", "--role", "user", "--content", "hello"],
     {"session_id": "old-chat", "message_id": "one", "role": "user", "content": "hello"})])
def test_generated_cli_sends_the_declared_json(tmp_path, capsys, action, args, body):
    def respond(handler, request):
        cli_send(handler, 200, api_v1.document() if request[1].endswith("openapi.json") else {"status": "ok", "result": {"ok": True}})
    server = Server(respond)
    try:
        client = tmp_path / "client.json"
        write_client_file(client, server.url)
        assert loopercli.main(["--client-file", str(client), "--json", "operator-chat", action.replace("_", "-"), *args]) == 0
        method, path, _, sent = server.requests[-1]
        assert (method, path, sent) == ("POST", f"/api/v1/operator_chat/{action}", body)
    finally:
        server.close()
