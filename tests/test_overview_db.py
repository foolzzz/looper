"""The dashboard's overview, the Tasks page's group filter and the status counts against a real PostgreSQL.

Runs only when LOOPER_TEST_DATABASE_URL points at a disposable database. Each
test works in a new schema of its own, dropped afterwards.
"""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

DSN = os.environ.get("LOOPER_TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")


def _run(test: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """Run ``test(pool)`` on a new schema that is also the server's pool."""
    import asyncpg

    from looper.db import connection

    async def _main() -> None:
        schema = f"looper_test_{uuid.uuid4().hex[:12]}"
        admin = await asyncpg.connect(DSN)
        await admin.execute(f'CREATE SCHEMA "{schema}"')
        pool = await asyncpg.create_pool(DSN, min_size=1, max_size=3, server_settings={"search_path": schema})
        try:
            await connection.ensure_schema(pool)
            monkeypatch.setattr(connection, "_pool", pool)
            await test(pool)
        finally:
            await pool.close()
            await admin.execute(f'DROP SCHEMA "{schema}" CASCADE')
            await admin.close()

    asyncio.run(_main())


class _Writer:
    def __init__(self) -> None:
        self.data = b""

    def write(self, data: bytes) -> None:
        self.data += data

    async def drain(self) -> None:
        pass


def _server(**parts: Any) -> Any:
    from looper.agents.http_server import AgentHTTPServer
    from looper.agents.tiers.task import TaskRouter

    supervisor = parts.pop("supervisor", SimpleNamespace(agent_status=lambda: {}))
    return AgentHTTPServer(web_adapter=SimpleNamespace(healthcheck=lambda: {}), supervisor=supervisor,
                           task_router=parts.pop("task_router", TaskRouter(runtime_root=Path(tempfile.mkdtemp()))),
                           **parts)


async def _get(server: Any, path: str, query: dict[str, str] | None = None) -> tuple[int, Any]:
    writer = _Writer()
    await server._route_api("GET", path, query or {}, b"", writer)
    head, _, payload = writer.data.partition(b"\r\n\r\n")
    return int(head.split()[1]), json.loads(payload)


async def _tasks(pool: Any, project_id: str, status: str, count: int) -> None:
    await pool.executemany(
        "INSERT INTO tasks (id, requirement, project_id, status) VALUES ($1, $2, $3, $4)",
        [(f"task-{uuid.uuid4().hex[:12]}", f"{status} work", project_id, status) for _ in range(count)],
    )


def test_every_task_is_counted_by_group_and_the_tasks_page_lists_a_group(monkeypatch: pytest.MonkeyPatch) -> None:
    from looper.db import projects

    async def test(pool: Any) -> None:
        a = await projects.create_project(name="alpha", pool=pool)
        b = await projects.create_project(name="beta", pool=pool)
        # More than the 200 rows the status count used to read, in every group and one unknown status.
        await _tasks(pool, a["id"], "completed", 205)
        for status, count in (("executing", 2), ("planning", 1), ("verifying", 1), ("assigned", 1), ("pending", 2),
                              ("init", 3), ("failed", 1), ("stopped", 1), ("cancelled", 1), ("archived", 1)):
            await _tasks(pool, b["id"], status, count)
        total = await pool.fetchval("SELECT count(*) FROM tasks")

        status, body = await _get(_server(), "/api/overview")
        assert status == 200
        assert body["tasks"] == {"in_progress": 5, "queued": 2, "ready": 3, "attention": 2, "completed": 205,
                                 "cancelled": 1, "other": 1, "total": total, "other_statuses": {"archived": 1}}
        # The CLI's status line reads the same counts.
        status, body = await _get(_server(), "/api/status")
        assert body["tasks"] == {"total": total, "running": 5, "pending": 2, "completed": 205}

        for group, statuses in (("attention", {"failed", "stopped"}), ("in_progress",
                                {"executing", "planning", "verifying", "assigned"}), ("other", {"archived"})):
            status, body = await _get(_server(), "/api/tasks", {"group": group})
            assert status == 200
            assert {t["status"] for t in body["tasks"]} == statuses
        status, body = await _get(_server(), "/api/tasks", {"group": "completed", "project_id": b["id"]})
        assert body["tasks"] == []
        status, body = await _get(_server(), "/api/tasks", {"group": "busy"})
        assert status == 400 and "in_progress" in body["error"]

    _run(test, monkeypatch)


def test_the_agents_running_now_and_each_active_projects_limits(monkeypatch: pytest.MonkeyPatch) -> None:
    from looper.db import agent_setting_layers, projects

    async def test(pool: Any) -> None:
        a = await projects.create_project(name="alpha", pool=pool)
        b = await projects.create_project(name="beta", pool=pool)
        c = await projects.create_project(name="gamma", pool=pool)
        await projects.create_project(name="delta", pool=pool)  # no tasks
        await _tasks(pool, a["id"], "executing", 1)
        await _tasks(pool, a["id"], "pending", 2)
        await _tasks(pool, b["id"], "init", 1)
        await _tasks(pool, c["id"], "completed", 4)
        await agent_setting_layers.put_layer("role", "planner", None, {"max_concurrency": 3}, pool=pool)
        await agent_setting_layers.put_layer("role_project", "worker", a["id"], {"max_concurrency": 2}, pool=pool)

        now = datetime.now(timezone.utc)
        router = SimpleNamespace(running_planners=lambda: [
            {"task_id": "task-a1", "planner_id": "planner-task-a1", "requirement": "build it", "name": "建解析器",
             "status": "running", "project_id": a["id"]}])
        run = {"task_id": "task-a1", "step_id": "step-1", "instruction": "write the parser", "project_id": a["id"]}
        pool_runs = [
            {**run, "agent_id": "worker-2", "role": "worker", "step": "tests", "state": "waiting", "since": now},
            {**run, "agent_id": "worker-1", "role": "worker", "step": "parser", "state": "running",
             "since": now - timedelta(minutes=12)},
            {**run, "agent_id": "reviewer-1", "role": "reviewer", "step": "parser", "state": "running", "since": now},
        ]
        down = {"healthy": False, "state": "restarting", "up": False, "restart_count": 2,
                "last_crash_at": "2026-10-15T12:00:00+00:00", "next_restart_at": "2026-10-15T12:00:04+00:00",
                "agent_type": "worker"}
        looping = {**down, "state": "crash_looping", "up": True, "restart_count": 3, "next_restart_at": None,
                   "agent_type": "task_router"}
        supervisor = SimpleNamespace(agent_status=lambda: {
            "manager": {"healthy": True, "state": "running", "up": True, "restart_count": 0, "last_crash_at": None,
                        "next_restart_at": None, "agent_type": "manager"},
            "worker-pool": down, "task-router": looping})
        server = _server(task_router=router, worker_pool=SimpleNamespace(runs=lambda: pool_runs),
                         manager=SimpleNamespace(agent_id="manager", busy=True), supervisor=supervisor)

        status, body = await _get(server, "/api/overview")
        assert status == 200
        agents = body["agents"]
        assert agents["manager"] == {"agent_id": "manager", "state": "busy"}
        assert [(p["agent_id"], p["project_name"], p["requirement"], p["name"]) for p in agents["planners"]] == [
            ("planner-task-a1", "alpha", "build it", "建解析器")]
        # Running ones first, each with how long it has been in its state.
        assert [(w["agent_id"], w["state"], w["step"]) for w in agents["workers"]] == [
            ("worker-1", "running", "parser"), ("worker-2", "waiting", "tests")]
        assert 720 <= agents["workers"][0]["elapsed_seconds"] < 780
        assert [r["agent_id"] for r in agents["reviewers"]] == ["reviewer-1"]
        # Only the projects with a task in progress, queued or ready, each against its limits read now.
        assert body["projects"] == [
            {"project_id": a["id"], "name": "alpha", "tasks": {"running": 1, "queued": 2, "ready": 0, "limit": 3},
             "workers": {"running": 1, "waiting": 1, "limit": 2},
             "reviewers": {"running": 1, "waiting": 0, "limit": 8}},
            {"project_id": b["id"], "name": "beta", "tasks": {"running": 0, "queued": 0, "ready": 1, "limit": 3},
             "workers": {"running": 0, "waiting": 0, "limit": 16},
             "reviewers": {"running": 0, "waiting": 0, "limit": 8}},
        ]
        assert body["idle_projects"] == 2
        # The components not simply running: one waiting to restart, one crash-looping though up now.
        assert body["health"] == {"unhealthy": [
            {"name": "worker-pool", **{k: down[k] for k in ("state", "up", "restart_count", "last_crash_at",
                                                            "next_restart_at")}},
            {"name": "task-router", **{k: looping[k] for k in ("state", "up", "restart_count", "last_crash_at",
                                                              "next_restart_at")}},
        ]}

        # Without a Manager, a router or a pool to ask, nothing runs.
        status, body = await _get(_server(), "/api/overview")
        assert body["agents"] == {"manager": None, "planners": [], "workers": [], "reviewers": []}
        assert [p["name"] for p in body["projects"]] == ["alpha", "beta"]

    _run(test, monkeypatch)


def test_todays_spend_keeps_money_apart_from_the_subscription_equivalent(monkeypatch: pytest.MonkeyPatch) -> None:
    from looper.agents.overview import local_midnight
    from looper.db import llm_records

    async def test(pool: Any) -> None:
        async def record(auth: str | None, source: str | None, cost: float | None, *,
                         status: str = "completed", at: datetime | None = None) -> None:
            record_id = await llm_records.start_record(
                agent_id="worker-1", task_id="task-1", pool=pool,
                metadata={"profile": {"provider": {"auth": auth}}} if auth else None)
            if status != "running":
                await llm_records.finish_record(record_id, status=status, cost_usd=cost, cost_source=source, pool=pool)
            if at is not None:
                await pool.execute("UPDATE llm_records SET created_at = $2 WHERE id = $1", record_id, at)

        await record("oauth", "runtime", 1.0)  # Claude Code's login
        await record("oauth", "provider_price", 0.5)  # Codex's login, at the built-in prices
        await record(None, "runtime", 0.25)  # a run from before Agent Profiles: Claude Code's login
        await record("api_key", "runtime", 0.2)  # Anthropic's API
        await record("api_key", "provider_price", 0.1)  # another provider's API
        await record("api_key", "provider_price", None)  # ended without a cost
        await record("api_key", None, None, status="running")
        await record("api_key", "provider_price", 9.0, at=local_midnight() - timedelta(minutes=1))  # yesterday

        status, body = await _get(_server(), "/api/overview")
        spend = body["spend_today"]
        assert datetime.fromisoformat(spend["since"]) == local_midnight()
        assert spend["provider_priced_usd"] == pytest.approx(0.3)
        assert spend["equivalent_usd"] == pytest.approx(1.75)
        assert (spend["runs"], spend["running"], spend["unpriced_runs"]) == (7, 1, 1)

    _run(test, monkeypatch)


def test_a_planning_or_verifying_task_is_active_for_the_sidebar_and_the_delete_guard(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from looper.db import projects

    async def test(pool: Any) -> None:
        project = await projects.create_project(name="alpha", pool=pool)
        for status in ("planning", "verifying", "executing", "pending", "init", "completed"):
            await pool.execute("INSERT INTO tasks (id, requirement, project_id, status) VALUES ($1, 'x', $2, $1)",
                               status, project["id"])

        status, body = await _get(_server(), "/api/projects")
        assert status == 200
        (listed,) = body["projects"]
        assert (listed["task_count"], listed["active_count"]) == (6, 4)
        status, body = await _get(_server(), "/api/tasks")
        assert {t["task_id"]: t["group"] for t in body["tasks"]} == {
            "planning": "in_progress", "verifying": "in_progress", "executing": "in_progress", "pending": "queued",
            "init": "ready", "completed": "completed"}

        server, writer = _server(), _Writer()
        await server._route_api("POST", "/api/tasks/delete", {}, json.dumps({"task_id": "planning"}).encode(), writer)
        assert writer.data.startswith(b"HTTP/1.1 400") and b"stop the task before deleting" in writer.data
        assert await pool.fetchval("SELECT count(*) FROM tasks WHERE id = 'planning'") == 1

    _run(test, monkeypatch)


def test_a_tasks_name_is_its_title_else_its_goal_else_its_requirement(monkeypatch: pytest.MonkeyPatch) -> None:
    async def test(pool: Any) -> None:
        from looper.db import projects, tasks

        project = await projects.create_project(name="alpha", work_dir="/w", pool=pool)
        await tasks.create_task(task_id="task-titled", requirement="Write hello.py.", goal_id="hello",
                                project_id=project["id"], title="  写 hello\n脚本 ", pool=pool)
        await tasks.create_task(task_id="task-goal", requirement="Write hello.py.", goal_id="hello-world",
                                project_id=project["id"], pool=pool)
        await tasks.create_task(task_id="task-plain", requirement="  Write hello.py.\nThen test it.",
                                project_id=project["id"], pool=pool)
        server = _server()
        status, body = await _get(server, "/api/tasks")
        assert status == 200
        assert {t["task_id"]: t["name"] for t in body["tasks"]} == {
            "task-titled": "写 hello 脚本", "task-goal": "hello-world", "task-plain": "Write hello.py."}
        status, detail = await _get(server, "/api/tasks/task-titled")
        assert (status, detail["name"], detail["requirement"]) == (200, "写 hello 脚本", "Write hello.py.")
        assert await pool.fetchval("SELECT title FROM tasks WHERE id = 'task-titled'") == "写 hello 脚本"

    _run(test, monkeypatch)
