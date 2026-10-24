"""Task edits use authenticated v1 requests, real services and disposable PostgreSQL readback."""

import json
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from looper.agents import api_v1
from looper.agents.agent_settings import CONCURRENCY_MAX
from looper.agents.bus import MessageBus
from looper.agents.message import TASK_ROUTER_ADDRESS, MessageType, channel_address
from looper.agents.ops.clients import ClientOps
from looper.agents.ops.service import Platform
from looper.agents.tiers.task import Task, TaskRouter
from looper.db import projects, task_changes, tasks
from tests.db_support import DSN, run_db
from tests.test_api_v1_auth import CLIENT_TOKEN, _bearer, _send, _serving, clients
from tests.test_operator_auth import TOKEN
from tests.test_ops_tasks_db import _project, _task
from tests.test_usage_api_db import _run_record

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")


@asynccontextmanager
async def _configured(tmp_path):
    async with _serving(tmp_path) as server:
        server._task_router = TaskRouter(runtime_root=tmp_path)
        server._supervisor = SimpleNamespace(bus=MessageBus(runtime_root=tmp_path))
        server._web_adapter = SimpleNamespace(_address=channel_address("web"))
        server._worker_pool = SimpleNamespace(recheck=AsyncMock())
        server._client_ops = ClientOps(Platform(bus=server._supervisor.bus, task_router=server._task_router,
                                               worker_pool=server._worker_pool, runtime_root=tmp_path))
        yield server
        await server._supervisor.bus.shutdown()


async def _post(server, action, data, *, operator=False, session="one"):
    status, _, body = await _send(server, f"POST /api/v1/{action}",
                                 {**_bearer(TOKEN if operator else CLIENT_TOKEN), "X-Looper-Session": session},
                                 json.dumps(data))
    return status, body


def _result(action, body):
    op = next(op for op in api_v1.operations() if op.operation_id == action)
    return api_v1.validate_arguments(op.result, body["result"])


def test_init_configuration_saves_requirement_versions_path_and_config(tmp_path, monkeypatch, clients):
    async def run(pool):
        project = await _project(pool)
        await _task(pool, "task-a", "init", project)
        async with _configured(tmp_path) as server:
            data = {"task_id": "task-a", "requirement": "Build calculator with tests",
                    "work_dir": str(tmp_path / "work" / ".." / "resolved"), "config": {"verify": True}}
            status, body = await _post(server, "tasks/configure", data)
            assert status == 200, body
            result = _result("tasks.configure", body)
            row = await tasks.get_task("task-a")
            assert result == {"task_id": "task-a", "requirement": data["requirement"],
                              "work_dir": str(tmp_path / "resolved"), "status": "init", "config": data["config"]}
            assert (row["requirement"], row["work_dir"], row["config"]) == (
                result["requirement"], result["work_dir"], result["config"])
            versions = await task_changes.requirement_versions("task-a")
            assert [(v["version"], v["text"]) for v in versions] == [(1, "build calc"), (2, data["requirement"])]
    run_db(run, monkeypatch, timeout=30)


@pytest.mark.parametrize("state", ["running", "failed", "completed"])
def test_non_init_configure_has_no_field_or_requirement_version_changes(tmp_path, monkeypatch, clients, state):
    async def run(pool):
        await _task(pool, "task-a", state, await _project(pool))
        before = await tasks.get_task("task-a")
        async with _configured(tmp_path) as server:
            status, body = await _post(server, "tasks/configure", {"task_id": "task-a", "requirement": "Changed",
                                                                  "work_dir": "/changed", "config": {"new": True}})
            assert status == 400 and body["error"]["code"] == "invalid"
            assert await tasks.get_task("task-a") == before
            assert await task_changes.requirement_versions("task-a") == []
    run_db(run, monkeypatch, timeout=30)


@pytest.mark.parametrize("state", list(tasks.STATUS_GROUPS))
def test_move_updates_database_and_live_router_in_every_valid_state(tmp_path, monkeypatch, clients, state):
    async def run(pool):
        old, new = await _project(pool), await _project(pool, "next")
        await _task(pool, "task-a", state, old)
        async with _configured(tmp_path) as server:
            server._task_router._tasks["task-a"] = Task("task-a", "build calc", "", "/w", old["id"], status=state)
            status, body = await _post(server, "tasks/move", {"task_id": "task-a", "project_id": new["id"]})
            assert status == 200, body
            assert _result("tasks.move", body) == {"task_id": "task-a", "project_id": new["id"], "project_name": "next"}
            assert (await tasks.get_task("task-a"))["project_id"] == new["id"]
            assert server._task_router._tasks["task-a"].project_id == new["id"]
            assert (await tasks.get_task("task-a"))["status"] == state
    run_db(run, monkeypatch, timeout=30)


@pytest.mark.parametrize("state", ["init", "running", "failed", "stopped", "cancelled", "completed"])
def test_agent_profile_limit_and_mode_set_and_clear_respect_completed_restriction(tmp_path, monkeypatch, clients, state):
    async def run(pool):
        await _task(pool, "task-a", state, await _project(pool))
        await pool.execute("INSERT INTO agent_profiles (id, name, runtime_id, model_id) VALUES "
                           "('profile-test', 'Selected', 'rt-claude-code', (SELECT id FROM llm_models "
                           "WHERE provider_id='prov-claude-subscription' AND name='sonnet'))")
        async with _configured(tmp_path) as server:
            data = {"task_id": "task-a", "profiles": {"worker": "profile-test"}, "concurrency": {"worker": CONCURRENCY_MAX},
                    "system_prompt_modes": {"worker": "replace"}}
            status, body = await _post(server, "tasks/agents", data)
            if state == "completed":
                assert status == 409 and body["error"]["code"] == "conflict"
                assert await pool.fetchval("SELECT count(*) FROM task_profile_overrides WHERE task_id='task-a'") == 0
                return
            assert status == 200, body
            role = _result("tasks.agents", body)["agents"]["roles"]["worker"]
            assert role["profile"]["value"]["profile_id"] == "profile-test"
            assert role["concurrency"]["task"] == CONCURRENCY_MAX
            assert role["system_prompt_mode"] == {"value": "replace", "source": "task"}
            server._worker_pool.recheck.assert_awaited_once()
            status, body = await _post(server, "tasks/agents", {"task_id": "task-a", "profiles": {"worker": None},
                "concurrency": {"worker": None}, "system_prompt_modes": {"worker": None}})
            assert status == 200, body
            role = _result("tasks.agents", body)["agents"]["roles"]["worker"]
            assert role["profile"]["source"] != "task" and role["concurrency"]["task"] is None
            assert role["system_prompt_mode"]["source"] != "task"
    run_db(run, monkeypatch, timeout=30)


async def _at_cap(pool):
    project = await _project(pool)
    await projects.update_project(project["id"], execution={"task_budget_usd": 0.4})
    await _task(pool, "task-a", "failed", project)
    await pool.execute("UPDATE tasks SET fail_kind='task_budget' WHERE id='task-a'")
    await _run_record(pool, "worker-one", task_id="task-a", cost=0.5)
    return project["id"]


async def _cap(pool):
    return await pool.fetchval("SELECT COALESCE(t.budget_usd,p.task_budget_usd) FROM tasks t "
                               "JOIN projects p ON p.id=t.project_id WHERE t.id='task-a'")

@pytest.mark.parametrize("state,kind,cap", [("running", "task_budget", 2), ("failed", None, 2),
    ("failed", "task_budget", 0.5), ("failed", "task_budget", 0.45), ("failed", "task_budget", 0)])
def test_continue_invalid_state_or_cap_never_writes_or_publishes(tmp_path, monkeypatch, clients, state, kind, cap):
    async def run(pool):
        project_id = await _at_cap(pool)
        await pool.execute("UPDATE tasks SET status=$1, fail_kind=$2 WHERE id='task-a'", state, kind)
        async with _configured(tmp_path) as server:
            status, body = await _post(server, "tasks/continue", {"task_id": "task-a", "task_budget_usd": cap}, operator=True)
            assert status == 400 and body["error"]["code"] == "invalid"
            assert (await projects.get_project(project_id))["task_budget_usd"] == 0.4
            assert server._supervisor.bus.get_queue(TASK_ROUTER_ADDRESS.agent_id).empty()
    run_db(run, monkeypatch, timeout=30)


@pytest.mark.parametrize("own_cap", [False, True])
def test_client_continue_waits_for_same_client_session_and_operator_executes_once(tmp_path, monkeypatch, clients, own_cap):
    async def run(pool):
        project_id = await _at_cap(pool)
        if own_cap:
            await tasks.set_task_budget("task-a", 0.4)
        async with _configured(tmp_path) as server:
            data = {"task_id": "task-a", "task_budget_usd": 2}
            status, asked = await _post(server, "tasks/continue", data)
            assert status == 202 and asked["status"] == "needs_confirmation"
            assert ("project" in asked["impact"].lower()) == (not own_cap)
            queue = server._supervisor.bus.get_queue(TASK_ROUTER_ADDRESS.agent_id)
            assert queue.empty() and (await _cap(pool)) == 0.4
            confirmation = {"confirmation_id": asked["confirmation_id"]}
            assert (await _post(server, "confirm", confirmation, session="other"))[0] == 404
            assert (await _post(server, "confirm", confirmation, operator=True))[0] == 404
            status, body = await _post(server, "confirm", confirmation)
            assert status == 200, body
            assert _result("tasks.continue", body) == {"task_id": "task-a", "project_id": project_id,
                "task_budget_usd": 2, "spent_usd": 0.5, "continued": "task-a"}
            assert (await _cap(pool)) == 2
            assert (await projects.get_project(project_id))["task_budget_usd"] == (0.4 if own_cap else 2)
            event = queue.get_nowait()
            assert event.message_type is MessageType.TASK_RESTART and event.payload == {"task_id": "task-a", "mode": "at_cap"}
            assert (await _post(server, "confirm", confirmation))[0] == 404 and queue.empty()
            status, body = await _post(server, "tasks/continue", {**data, "task_budget_usd": 3}, operator=True)
            assert status == 200 and body["result"]["task_budget_usd"] == 3
            assert queue.qsize() == 1
    run_db(run, monkeypatch, timeout=30)


def test_existing_concurrency_operation_returns_actual_typed_changes(tmp_path, monkeypatch, clients):
    async def run(pool):
        await _task(pool, "task-a", "running", await _project(pool))
        async with _configured(tmp_path) as server:
            status, body = await _post(server, "ops/concurrency", {"task_id": "task-a", "limits": {"worker": 3}})
            assert status == 200, body
            change, = _result("concurrency", body)["changes"]
            assert change["role"] == "worker" and change["after"] == {"value": 3, "source": "task"}
            assert change["task_limit"] == 3 and change["default"]["value"] > 0
    run_db(run, monkeypatch, timeout=30)


def test_failed_config_write_never_reports_success(tmp_path, monkeypatch, clients):
    async def run(pool):
        await _task(pool, "task-a", "init", await _project(pool))
        before = await tasks.get_task("task-a")
        monkeypatch.setattr(tasks, "update_task_config", AsyncMock(return_value=False))
        async with _configured(tmp_path) as server:
            status, body = await _post(server, "tasks/configure", {"task_id": "task-a", "config": {"new": True}})
            assert status == 503 and body["error"]["code"] == "unavailable"
            assert await tasks.get_task("task-a") == before
    run_db(run, monkeypatch, timeout=30)


def test_abandoned_continue_is_replaced_without_writes_or_dispatch(tmp_path, monkeypatch, clients):
    async def run(pool):
        await _at_cap(pool)
        async with _configured(tmp_path) as server:
            data = {"task_id": "task-a", "task_budget_usd": 2}
            status, abandoned = await _post(server, "tasks/continue", data)
            assert status == 202
            status, replacement = await _post(server, "tasks/continue", {**data, "task_budget_usd": 3})
            assert status == 202 and replacement["confirmation_id"] != abandoned["confirmation_id"]
            assert (await _cap(pool)) == 0.4
            assert server._supervisor.bus.get_queue(TASK_ROUTER_ADDRESS.agent_id).empty()
            assert (await _post(server, "confirm", {"confirmation_id": abandoned["confirmation_id"]}))[0] == 404
            status, body = await _post(server, "confirm", {"confirmation_id": replacement["confirmation_id"]})
            assert status == 200 and body["result"]["task_budget_usd"] == 3
            assert server._supervisor.bus.get_queue(TASK_ROUTER_ADDRESS.agent_id).qsize() == 1
    run_db(run, monkeypatch, timeout=30)


@pytest.mark.parametrize("action,method,suffix,data", [
    ("configure", "PUT", "config", {"requirement": "Edited"}),
    ("move", "PUT", "project", {}), ("agents", "PUT", "agents", {"concurrency": {"worker": 2}}),
    ("continue", "POST", "continue", {"task_budget_usd": 2}),
])
def test_retired_routes_are_404_with_successful_v1_control(tmp_path, monkeypatch, clients, action, method, suffix, data):
    async def run(pool):
        project_id = await _at_cap(pool)
        if action != "continue":
            await pool.execute("UPDATE tasks SET status='init' WHERE id='task-a'")
        if action == "move":
            data["project_id"] = project_id
        async with _configured(tmp_path) as server:
            status, body = await _post(server, f"tasks/{action}", {"task_id": "task-a", **data}, operator=True)
            assert status == 200, body
            status, _, _ = await _send(server, f"{method} /api/tasks/task-a/{suffix}", _bearer(TOKEN), json.dumps(data))
            assert status == 404
    run_db(run, monkeypatch, timeout=30)


@pytest.mark.parametrize("change", ["project", "own_cap"])
def test_continue_confirmation_refuses_a_changed_budget_scope(tmp_path, monkeypatch, clients, change):
    async def run(pool):
        original = await _at_cap(pool)
        other = await _project(pool, "other")
        async with _configured(tmp_path) as server:
            _, pending = await _post(server, "tasks/continue", {"task_id": "task-a", "task_budget_usd": 2})
            if change == "project":
                assert (await _post(server, "tasks/move", {"task_id": "task-a", "project_id": other["id"]}))[0] == 200
            else:
                await tasks.set_task_budget("task-a", 0.6)
            status, body = await _post(server, "confirm", {"confirmation_id": pending["confirmation_id"]})
            assert status == 409 and body["error"]["code"] == "conflict"
            assert (await projects.get_project(original))["task_budget_usd"] == 0.4
            assert (await projects.get_project(other["id"]))["task_budget_usd"] is None
            assert server._supervisor.bus.get_queue(TASK_ROUTER_ADDRESS.agent_id).empty()
    run_db(run, monkeypatch, timeout=30)
