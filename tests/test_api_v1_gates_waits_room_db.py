"""Task interaction readbacks and retired routes on disposable PostgreSQL."""

import json
from contextlib import asynccontextmanager
from types import SimpleNamespace
from datetime import datetime, timezone

import pytest

from looper.agents import api_v1
from looper.agents.room import MemberKind, RoomService, Seat
from looper.agents.message import channel_address
from looper.agents.ops.clients import ClientOps
from looper.agents.ops.service import Platform
from looper.db import api_clients, plans, projects, room as db_room, task_roles, tasks
from tests.db_support import DSN, run_db
from tests.test_api_v1_auth import _bearer, _send, _serving
from tests.test_operator_auth import TOKEN

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")


BACKEND = Seat("backend_rd@s1", "backend_rd", MemberKind.WORK, step_id="step-s1", step_key="s1", run="worker-1")
FE = Seat("fe_rd@s2", "fe_rd", MemberKind.WORK, step_id="step-s2", step_key="s2", run="worker-2")


class _Bus:
    def __init__(self):
        self.sent = []

    async def publish(self, message):
        self.sent.append(message)


async def _seed(pool, *, kind="gate", task_id="task-a"):
    project = await projects.create_project(name="Example", work_dir="/example", pool=pool)
    await tasks.create_task(task_id=task_id, requirement="Build it", project_id=project["id"],
                            work_dir="/example", status="running", pool=pool)
    if kind == "room":
        for index, seat in enumerate((BACKEND, FE)):
            await pool.execute("INSERT INTO task_steps (id, task_id, step_index, step_key, instruction, status, "
                "worker_id, role_key) VALUES ($1,$2,$3,$4,'Build it','dispatched',$5,$6)",
                seat.step_id, task_id, index, seat.step_key, seat.run, seat.role)
    elif kind in ("gate", "wait"):
        key = "g1" if kind == "gate" else "w1"
        common = {"step_id": "step-" + key, "topic": "Choose storage", "state": "open" if kind == "gate" else "waiting"}
        detail = ({"agenda": [{"id": "q1", "question": "Which database?", "options": ["PostgreSQL", "SQLite"],
                                 "status": "open", "answer": ""}]} if kind == "gate" else
                  {"spec": {"max_wait_seconds": 60, "not_before_seconds": 1, "not_before": None,
                             "check": {"type": "command", "command": "true"}, "every_seconds": 1},
                   "opened_at": datetime.now(timezone.utc)})
        await plans.save(task_id, steps=[{"id": "step-" + key, "index": 0, "key": key,
                                         "instruction": "Build it", "kind": kind}],
                         **{"gates" if kind == "gate" else "waits": [{**common, **detail}]}, pool=pool)


@asynccontextmanager
async def _api(tmp_path, pool, *, live=False, workers=None):
    _, token = await api_clients.create("task interaction validation", pool=pool)
    async with _serving(tmp_path) as server:
        bus = _Bus()
        server._supervisor = SimpleNamespace(bus=bus)
        server._task_router = SimpleNamespace(has_planner=lambda _: live)
        server._worker_pool = workers
        server._web_adapter = SimpleNamespace(_address=channel_address("web"))
        server._client_ops = ClientOps(Platform(bus=bus, task_router=server._task_router, worker_pool=workers))
        async def request(name, body=None, *, client=False, method="POST"):
            path = name if name.startswith("/") else "/api/v1/" + name.replace(".", "/")
            status, _, answer = await _send(server, f"{method} {path}",
                _bearer(token if client else TOKEN), json.dumps(body or {}))
            if status == 200 and path.startswith("/api/v1/"):
                api_v1.validate_arguments({"type": "object", "properties": {
                    "result": api_v1.find(method, path).result}}, {"result": answer["result"]})
            return status, answer
        yield request, bus


@pytest.mark.parametrize(("method", "suffix", "operation"), [
    ("GET", "/api/gates", "ops.gates.list"),
    ("GET", "/api/tasks/task-a/gates/g1/messages", "ops.gates.messages"),
    ("POST", "/api/tasks/task-a/gates/g1/messages", "ops.gates.reply"),
    ("POST", "/api/tasks/task-a/gates/g1/end", "ops.gates.end"),
    ("POST", "/api/tasks/task-a/waits/w1/resume", "waits.resume"),
    ("POST", "/api/tasks/task-a/waits/w1/cancel", "waits.cancel"),
    ("PUT", "/api/tasks/task-a/roles/tester", "tasks.role_update"),
    ("GET", "/api/tasks/task-a/room", "tasks.room_view"),
])
@pytest.mark.parametrize("client", [False, True], ids=["operator", "client"])
def test_retired_routes_have_authenticated_successful_v1_controls(tmp_path, monkeypatch, method, suffix, operation, client):
    async def run(pool):
        if operation.startswith("waits."):
            await _seed(pool, kind="wait")
            body = {"task_id": "task-a", "step": "w1"}
        else:
            await _seed(pool, kind="gate" if operation.startswith("ops.gates.") else "task")
            body = {"task_id": "task-a"}
            if operation.startswith("ops.gates."):
                body.update(gate="g1", **({"text": "PostgreSQL"} if operation.endswith("reply") else {}))
                if operation.endswith("list"):
                    body = {"limit": 1, "offset": 0}
            elif operation == "tasks.role_update":
                await pool.execute("INSERT INTO task_roles (task_id,role_key,version,kind,role_prompt) "
                                   "VALUES ('task-a','tester',1,'work','first')")
                body.update(key="tester", role_prompt="Check edge cases.", harness=None, profile_id=None)
        async with _api(tmp_path, pool, live=True) as (call, bus):
            assert (await call(suffix, method=method))[0] == 404
            status, answer = await call(operation, body, client=client)
            assert status == 200 and answer["status"] == "ok", answer
            assert answer["result"]
            if operation.startswith("waits.") or operation == "tasks.role_update":
                client_id = await pool.fetchval("SELECT id FROM api_clients") if client else None
                assert bus.sent[0].source == (channel_address("api", client_id) if client else channel_address("web"))
                audit = await pool.fetchrow("SELECT client_id, status FROM manager_actions ORDER BY id DESC LIMIT 1")
                assert dict(audit) == {"client_id": client_id, "status": "ok"}
    run_db(run, monkeypatch, timeout=30)


@pytest.mark.parametrize("client", [False, True], ids=["operator", "client"])
@pytest.mark.parametrize(("action", "extra"), [("reply", {"text": "Full answer", "agenda_item": "q1",
                                                      "option": "PostgreSQL"}), ("end", {"text": "Conclude."})])
def test_gate_writes_preserve_queued_text_and_authenticated_via(tmp_path, monkeypatch, client, action, extra):
    async def run(pool):
        await _seed(pool)
        body = {"task_id": "task-a", "gate": "g1"}
        async with _api(tmp_path, pool) as (call, bus):
            status, answer = await call("ops.gates." + action, {**body, **extra}, client=client)
            assert status == 200 and answer["result"]["delivered"] == "kept", answer
            assert not bus.sent
            status, thread = await call("ops.gates.messages", body, client=client)
            assert status == 200 and thread["result"]["messages"][0]["text"] == extra["text"]
            via = thread["result"]["messages"][0]["via"]
            assert via == ("client:task interaction validation" if client else "dashboard")
            await pool.execute("UPDATE task_user_gates SET state = 'cancelled'")
            status, answer = await call("ops.gates." + action, {**body, **extra}, client=client)
            assert status == 409 and answer["error"]["code"] == "conflict"
    run_db(run, monkeypatch, timeout=30)


def test_full_room_filters_consults_and_compact_clipping(tmp_path, monkeypatch):
    async def run(pool):
        await _seed(pool, kind="room", task_id="task-r")
        workers = SimpleNamespace(room_runs=lambda _: {"worker-1": ("running", datetime.now(timezone.utc)),
                                                      "worker-2": ("running", datetime.now(timezone.utc))})
        room = RoomService(worker_pool=workers)
        body = "Complete\nanswer: " + ("details " * 100).rstrip()
        first = await room.post("task-r", FE, kind="question", body=body, to=["backend_rd"])
        second = await room.post("task-r", BACKEND, kind="question", body="Still unanswered?", to=["fe_rd"])
        await room.post("task-r", BACKEND, kind="answer", body="Yes", reply_to=first["id"])
        consult, _ = await db_room.admit_consult("task-r", seat=BACKEND.key, question=first["id"], source_run="worker-1",
                                                source_record=None, runtime="codex", limit=2)
        await db_room.move_consult(consult["id"], "answered", expect=["queued"], read_only="runtime",
                                   denied_writes=2, cost_usd=0.25)
        async with _api(tmp_path, pool, workers=workers) as (call, _):
            status, answer = await call("tasks.room_view", {"task_id": "task-r"})
            full = answer["result"]
            assert status == 200 and full["messages"][0]["body"] == body
            consults = full["consults"]
            assert (consults["total"], consults["answered"], consults["cost_usd"]) == (1, 1, 0.25)
            assert {k: consults["list"][0][k] for k in ("questions", "read_only", "denied_writes", "cost_usd")} == {
                "questions": [first["id"]], "read_only": "runtime", "denied_writes": 2, "cost_usd": 0.25}
            for fields, expected in [({"kind": "answer"}, ["Yes"]), ({"from": "fe_rd"}, [body]),
                                     ({"unanswered": True}, ["Still unanswered?"]),
                                     ({"after": second["id"], "limit": 1}, ["Yes"])]:
                status, filtered = await call("tasks.room_view", {"task_id": "task-r", **fields})
                assert status == 200 and [m["body"] for m in filtered["result"]["messages"]] == expected
            _, page = await call("tasks.room_view", {"task_id": "task-r", "limit": 1})
            assert page["result"]["more"] and page["result"]["messages"][0]["body"] == "Yes"
            status, compact = await call("ops.tasks.room", {"task_id": "task-r"})
            assert status == 200 and len(compact["result"]["messages"][0]["body"]) <= 500
            assert compact["result"]["messages"][0]["body"] != body
    run_db(run, monkeypatch, timeout=30)


def test_role_version_race_returns_conflict_without_wakeup(tmp_path, monkeypatch):
    async def run(pool):
        await _seed(pool, kind="task")
        await pool.execute("INSERT INTO task_roles (task_id,role_key,version,kind,role_prompt) "
                           "VALUES ('task-a','tester',1,'work','first')")
        original = task_roles.add_version
        async def competing(task_id, key, values, entries, **kwargs):
            assert await original(task_id, key, {**values, "role_prompt": "Concurrent edit"}, entries, **kwargs) == 2
            return await original(task_id, key, values, entries, **kwargs)
        monkeypatch.setattr(task_roles, "add_version", competing)
        async with _api(tmp_path, pool, live=True) as (call, bus):
            status, answer = await call("tasks.role_update", {"task_id": "task-a", "key": "tester",
                "role_prompt": "Check it.", "harness": None, "profile_id": None})
            assert status == 409 and answer["error"]["code"] == "conflict" and not bus.sent
            assert await pool.fetchval("SELECT role_prompt FROM task_roles WHERE version=2") == "Concurrent edit"
            assert await pool.fetchval("SELECT count(*) FROM task_roles") == 2
    run_db(run, monkeypatch, timeout=30)


@pytest.mark.parametrize(("name", "body"), [
    ("waits.resume", {"task_id": 1, "step": "w1"}),
    ("waits.cancel", {"task_id": "task-a", "step": None}),
    ("waits.resume", {"task_id": "task-a", "step": "w1", "via": "dashboard"}),
    ("tasks.role_update", {"task_id": "t", "key": "r", "role_prompt": "x", "harness": None}),
    ("tasks.role_update", {"task_id": "t", "key": "r", "role_prompt": "x", "harness": None,
                           "profile_id": None, "via": "user"}),
    *[("tasks.room_view", {"task_id": "t", **fields}) for fields in
      ({"kind": "unknown"}, {"unanswered": "true"}, {"after": -1}, {"limit": 0}, {"limit": 501})],
])
@pytest.mark.parametrize("client", [False, True], ids=["operator", "client"])
def test_interaction_requests_reject_undeclared_or_wrong_typed_input(tmp_path, monkeypatch, name, body, client):
    async def run(pool):
        await _seed(pool, kind="task")
        async with _api(tmp_path, pool, live=True) as (call, bus):
            status, view = await call("tasks.room_view", {"task_id": "task-a"}, client=client)
            assert status == 200 and view["result"]["counts"]["messages"] == 0
            status, answer = await call(name, body, client=client)
            assert status == 400 and answer["error"]["code"] == "invalid" and not bus.sent
            assert await pool.fetchval("SELECT count(*) FROM task_roles") == 0
    run_db(run, monkeypatch, timeout=30)


@pytest.mark.parametrize(("case", "expected"), [("completed", 409), ("no_change", 400), ("missing_profile", 400)])
def test_refused_role_edits_preserve_current_version(tmp_path, monkeypatch, case, expected):
    async def run(pool):
        await _seed(pool, kind="task")
        await pool.execute("INSERT INTO task_roles (task_id,role_key,version,kind,role_prompt) "
                           "VALUES ('task-a','tester',1,'work','first')")
        if case == "completed":
            await pool.execute("UPDATE tasks SET status='completed'")
        body = {"task_id": "task-a", "key": "tester", "role_prompt": "first" if case == "no_change" else "Changed",
                "harness": None, "profile_id": "missing-profile" if case == "missing_profile" else None}
        async with _api(tmp_path, pool, live=True) as (call, bus):
            status, answer = await call("tasks.role_update", body)
            assert status == expected and answer["error"]["code"] == ("conflict" if expected == 409 else "invalid")
            assert not bus.sent and await pool.fetchval("SELECT count(*) FROM task_roles") == 1
    run_db(run, monkeypatch, timeout=30)
