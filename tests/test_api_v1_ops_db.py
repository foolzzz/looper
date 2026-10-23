"""``/api/v1`` operations over HTTP routing on a real PostgreSQL: a read, a confirmed write, the refusals and the
document. Runs only when LOOPER_TEST_DATABASE_URL points at a disposable database."""

from __future__ import annotations

import json
import logging
import tempfile
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from looper.agents.api_v1 import OPERATOR, Principal
from looper.agents.bus import MessageBus
from looper.agents.ops.clients import ClientOps
from looper.agents.ops.service import Platform
from tests.test_ops_tasks_db import _project
from tests.db_support import DSN, run_db
from tests.test_usage_api_db import _Writer

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")


async def _v1(method: str, path: str, body: Any = None, *, principal: Principal = OPERATOR,
              session: str | None = None, ops: ClientOps | None = None, raw: bytes | None = None) -> tuple[int, Any]:
    from looper.agents.http_server import AgentHTTPServer
    from looper.agents.tiers.task import TaskRouter

    server = AgentHTTPServer(web_adapter=SimpleNamespace(), supervisor=SimpleNamespace(),
                             task_router=TaskRouter(runtime_root=Path(tempfile.mkdtemp())), client_ops=ops)
    writer = _Writer()
    payload = raw if raw is not None else b"" if body is None else json.dumps(body).encode()
    headers = {"x-looper-session": session} if session else {}
    await server._handle_v1(method, path, {}, headers, payload, writer, principal)
    head, _, text = writer.data.partition(b"\r\n\r\n")
    assert b"Cache-Control: no-store" in head
    return int(head.split()[1]), json.loads(text)


def _ops(tmp: Path) -> ClientOps:
    router = SimpleNamespace(has_planner=lambda task_id: False)
    return ClientOps(Platform(bus=MessageBus(runtime_root=tmp), task_router=router))


def test_a_read_a_wrong_method_and_an_unknown_path(monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
                                                  caplog: pytest.LogCaptureFixture) -> None:
    async def test(pool: Any) -> None:
        ops = _ops(tmp_path)
        with caplog.at_level(logging.INFO, logger="looper.agents.http_server"):
            status, answer = await _v1("POST", "/api/v1/ops/tasks/list", {}, ops=ops)
        assert status == 200 and answer["status"] == "ok" and answer["result"] == {"tasks": [], "more": False}
        assert "api client=operator op=tasks.list status=ok ms=" in caplog.text
        status, answer = await _v1("GET", "/api/v1/ops/tasks/list", ops=ops)
        assert (status, answer["error"]) == (405, {"code": "invalid", "message": "use POST"})
        status, answer = await _v1("POST", "/api/v1/ops/nope", {}, ops=ops)
        assert status == 404 and answer["error"]["code"] == "not_found"
        status, answer = await _v1("POST", "/api/v1/chat/conversations", {}, ops=ops)
        assert status == 403 and answer["error"]["code"] == "forbidden"  # the chat is for API clients
        status, answer = await _v1("POST", "/api/v1/ops/tasks/list", {})
        assert status == 503 and answer["error"]["code"] == "unavailable"

    run_db(test, monkeypatch)


def test_a_write_waits_for_the_same_session_to_confirm(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from looper.db import api_clients, projects, tasks

    async def test(pool: Any) -> None:
        await _project(pool)
        view, _ = await api_clients.create("agent", pool=pool)
        client = Principal("client", view["id"], view["name"])
        ops = _ops(tmp_path)
        status, asked = await _v1("POST", "/api/v1/ops/task_create", {
            "requirement": "build tetris", "title": "Tetris", "project": "Tetris", "work_dir": "/srv/tetris"},
            principal=client, session="s1", ops=ops)
        assert status == 202 and asked["status"] == "needs_confirmation"
        assert await projects.find_project_by_name("Tetris", pool=pool) is None

        status, answer = await _v1("POST", "/api/v1/confirm", {"confirmation_id": asked["confirmation_id"]},
                                   principal=client, session="s2", ops=ops)
        assert status == 404 and answer["error"]["code"] == "not_found"
        status, answer = await _v1("POST", "/api/v1/confirm", {}, principal=client, session="s1", ops=ops)
        assert status == 400 and answer["error"]["code"] == "invalid"
        status, answer = await _v1("POST", "/api/v1/confirm", {"confirmation_id": 7}, principal=client,
                                   session="s1", ops=ops)
        assert status == 400
        status, done = await _v1("POST", "/api/v1/confirm", {"confirmation_id": asked["confirmation_id"]},
                                 principal=client, session="s1", ops=ops)
        assert status == 200 and done["result"]["project_created"] is True
        row = await tasks.get_task(done["result"]["task_id"], pool=pool)
        assert row["client_id"] == client.client_id

    run_db(test, monkeypatch)


def test_a_body_that_is_not_an_object_or_names_the_action(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    async def test(pool: Any) -> None:
        ops = _ops(tmp_path)
        status, answer = await _v1("POST", "/api/v1/ops/tasks/list", {"action": "get"}, ops=ops)
        assert status == 400 and answer["error"]["message"] == "action is part of the path"
        for raw in (b"[]", b"not json", b'"x"'):
            status, answer = await _v1("POST", "/api/v1/ops/tasks/list", raw=raw, ops=ops)
            assert status == 400 and answer["error"]["message"] == "the body must be a JSON object"
        status, answer = await _v1("POST", "/api/v1/ops/tasks/list", {"limit": "x"}, ops=ops)
        assert status == 400 and answer["error"]["code"] == "invalid"

    run_db(test, monkeypatch)


def test_the_document_is_served(monkeypatch: pytest.MonkeyPatch) -> None:
    async def test(pool: Any) -> None:
        status, answer = await _v1("GET", "/api/v1/openapi.json")
        assert status == 200 and "/ops/tasks/list" in answer["paths"] and answer["openapi"] == "3.1.0"

    run_db(test, monkeypatch)
