"""Task controls and edits through authenticated v1 HTTP, read back from disposable PostgreSQL."""

import json
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from looper.agents import api_v1
from looper.agents.bus import MessageBus
from looper.agents.message import MessageType, channel_address
from looper.agents.ops.clients import ClientOps
from looper.agents.ops.service import Platform
from looper.db import api_clients, journal, projects, task_changes, tasks
from tests.db_support import DSN, run_db
from tests.test_api_v1_auth import _bearer, _send, _serving
from tests.test_operator_auth import TOKEN

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")


async def _task(pool, state):
    project = await projects.create_project(name="Demo", work_dir="/demo", pool=pool)
    await tasks.create_task(task_id="task-a", status=state, requirement="build calc",
                            project_id=project["id"], work_dir="/demo", pool=pool)


@asynccontextmanager
async def _controls(tmp_path, *, planner=False):
    async with _serving(tmp_path) as server:
        bus = MessageBus(runtime_root=tmp_path)
        server._task_router = SimpleNamespace(has_planner=lambda _: planner)
        server._supervisor = SimpleNamespace(bus=bus)
        server._web_adapter = SimpleNamespace(_address=channel_address("web", "web"))
        server._client_ops = ClientOps(Platform(bus=bus, task_router=server._task_router))

        async def post(path, data, *, token=TOKEN, session="one"):
            status, _, answer = await _send(server, "POST " + path,
                {**_bearer(token), "X-Looper-Session": session}, json.dumps(data))
            return status, answer

        yield server, bus, post


def _messages(bus, address="task-router"):
    queue, messages = bus.get_queue(address), []
    while not queue.empty():
        messages.append(queue.get_nowait())
    return messages


@pytest.mark.parametrize(("action", "state", "message", "mode", "legacy"), [
    ("start", "init", MessageType.TASK_START, None, "/api/tasks/task-a/start"),
    ("stop", "waiting_user", MessageType.TASK_STOP, None, "/api/tasks/stop"),
    ("restart", "stopped", MessageType.TASK_RESTART, "continue", "/api/tasks/start"),
    ("start_over", "failed", MessageType.TASK_RESTART, "start_over", "/api/tasks/start"),
    ("cancel", "executing", MessageType.TASK_CANCEL, None, "/api/tasks/cancel"),
])
def test_operator_control_replaces_legacy_and_dispatches_once(tmp_path, monkeypatch, action, state, message, mode, legacy):
    async def run(pool):
        await _task(pool, state)
        async with _controls(tmp_path) as (server, bus, post):
            status, _, _ = await _send(server, "POST " + legacy, _bearer(TOKEN), '{"task_id":"task-a"}')
            assert status == 404
            status, answer = await post("/api/v1/ops/task_control/" + action, {"task_id": "task-a"})
            assert status == 200 and answer["status"] == "ok", answer
            operation = next(op for op in api_v1.operations() if op.operation_id == "task_control." + action)
            api_v1.validate_arguments(operation.result, answer["result"])
            assert [(e.message_type, e.payload) for e in _messages(bus)] == [
                (message, {"task_id": "task-a", **({"mode": mode} if mode else {})})]
    run_db(run, monkeypatch, timeout=20)


@pytest.mark.parametrize(("action", "state"), [
    ("start", "stopped"), ("stop", "init"), ("restart", "completed"),
    ("start_over", "verifying"), ("cancel", "init"),
])
def test_invalid_transition_is_409_and_never_dispatches(tmp_path, monkeypatch, action, state):
    async def run(pool):
        await _task(pool, state)
        async with _controls(tmp_path) as (_, bus, post):
            status, answer = await post("/api/v1/ops/task_control/" + action, {"task_id": "task-a"})
            assert status == 409 and answer["error"]["code"] == "conflict"
            assert _messages(bus) == []
    run_db(run, monkeypatch, timeout=20)


@pytest.mark.parametrize(("action", "state"), [("cancel", "executing"), ("start_over", "failed")])
def test_high_client_control_waits_for_same_principal_and_session(tmp_path, monkeypatch, action, state):
    async def run(pool):
        await _task(pool, state)
        _, token = await api_clients.create("first", pool=pool)
        _, other = await api_clients.create("other", pool=pool)
        async with _controls(tmp_path) as (_, bus, post):
            status, pending = await post("/api/v1/ops/task_control/" + action, {"task_id": "task-a"}, token=token)
            assert status == 202 and pending["status"] == "needs_confirmation"
            assert "task-a" in pending["impact"] and pending["expires_in_seconds"] > 0
            assert _messages(bus) == []
            body = {"confirmation_id": pending["confirmation_id"]}
            for wrong in ({"session": "two", "token": token}, {"token": other}, {}):
                status, answer = await post("/api/v1/confirm", body, **wrong)
                assert status == 404 and answer["error"]["code"] == "not_found"
                assert _messages(bus) == []
            status, answer = await post("/api/v1/confirm", body, token=token)
            assert status == 200 and answer["status"] == "ok"
            assert len(_messages(bus)) == 1
            assert (await post("/api/v1/confirm", body, token=token))[0] == 404
            assert _messages(bus) == []
    run_db(run, monkeypatch, timeout=20)


def test_title_is_idempotent_and_requirement_versions_are_written_once(tmp_path, monkeypatch):
    async def run(pool):
        await _task(pool, "executing")
        async with _controls(tmp_path, planner=True) as (server, bus, post):
            for path, data in (("title", {"title": "Demo"}), ("requirement", {"requirement": "Build v2"})):
                status, _, _ = await _send(server, "PUT /api/tasks/task-a/" + path, _bearer(TOKEN), json.dumps(data))
                assert status == 404
            for _ in range(2):
                status, answer = await post("/api/v1/ops/task_update", {"task_id": "task-a", "title": "  Demo\n task "})
                assert status == 200 and answer["result"] == {"task_id": "task-a", "title": "Demo task", "changed": ["title"]}
            notes = await journal.list_entries("task-a", pool=pool)
            assert len(notes) == 1 and notes[0]["delivered_at"] is not None
            assert (await tasks.get_task("task-a", pool=pool))["title"] == "Demo task"
            status, answer = await post("/api/v1/tasks/requirement_update", {"task_id": "task-a", "requirement": "Build v2"})
            assert status == 200 and answer["result"] == {"task_id": "task-a", "version": 2, "requirement": "Build v2"}
            versions = await task_changes.requirement_versions("task-a", pool=pool)
            assert [(v["version"], v["text"]) for v in versions] == [(1, "build calc"), (2, "Build v2")]
            assert versions[0]["delivered_at"] is not None and versions[1]["delivered_at"] is None
            assert (await tasks.get_task("task-a", pool=pool))["requirement"] == "Build v2"
            assert len(await journal.list_entries("task-a", pool=pool)) == 1
            assert [e.message_type for e in _messages(bus, "planner-task-a")] == [MessageType.TASK_CHANGE]
    run_db(run, monkeypatch, timeout=20)


@pytest.mark.parametrize(("state", "body", "code"), [
    ("completed", {"requirement": "Build v2"}, 409),
    ("init", {"requirement": "build calc"}, 400),
    *[("init", {"requirement": text}, 400) for text in ("", "  ", None, 7, "x" * 32001)],
    ("init", {"requirement": "Build v2", "extra": True}, 400),
    ("init", {}, 400),
    *[("init", {"task_id": value, "requirement": "Build v2"}, 400) for value in (None, 7, "")],
])
def test_requirement_refusal_never_changes_task_or_history(tmp_path, monkeypatch, state, body, code):
    async def run(pool):
        await _task(pool, state)
        before = await tasks.get_task("task-a", pool=pool)
        async with _controls(tmp_path, planner=True) as (_, bus, post):
            status, answer = await post("/api/v1/tasks/requirement_update", {"task_id": "task-a", **body})
            assert status == code and answer["status"] == "error"
            assert await tasks.get_task("task-a", pool=pool) == before
            assert await task_changes.requirement_versions("task-a", pool=pool) == []
            assert await journal.list_entries("task-a", pool=pool) == []
            assert _messages(bus, "planner-task-a") == []
    run_db(run, monkeypatch, timeout=20)


def test_client_requirement_edit_writes_one_version_and_audit(tmp_path, monkeypatch):
    async def run(pool):
        await _task(pool, "init")
        client, token = await api_clients.create("editor", pool=pool)
        async with _controls(tmp_path) as (_, bus, post):
            status, answer = await post("/api/v1/tasks/requirement_update",
                                       {"task_id": "task-a", "requirement": "Build v2"}, token=token)
            assert status == 200 and answer["result"]["version"] == 2
            assert len(await task_changes.requirement_versions("task-a", pool=pool)) == 2
            assert (await task_changes.pending("task-a", pool=pool)).empty
            rows = await pool.fetch("SELECT client_id, status, arguments FROM manager_actions")
            assert len(rows) == 1 and (rows[0]["client_id"], rows[0]["status"]) == (client["id"], "ok")
            assert _messages(bus) == []
    run_db(run, monkeypatch, timeout=20)
