"""Raising the spend cap of a task that failed at it, and continuing it, against a real PostgreSQL.

``POST /api/tasks/<id>/continue`` takes the new cap per task. It is taken
only for a task that failed at its cap and only above what the task has
spent; then the project's cap is raised and the task restarts from its saved
plan, its steps kept. Runs only when LOOPX_TEST_DATABASE_URL points at a
disposable database.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from loopx.agents.http_server import AgentHTTPServer
from loopx.agents.message import TASK_ROUTER_ADDRESS, MessageEnvelope, MessageType, channel_address, planner_address
from loopx.agents.tiers.task import Task, TaskRouter
from tests.test_usage_api_db import DSN, _run, _run_record, _Writer

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPX_TEST_DATABASE_URL is not set")

CAP_REASON = "任务失败:\nTask spend cap reached: $0.5 spent of the $0.4 cap (task_budget_usd)"


async def _failed_task(pool: Any, router: TaskRouter, fail_kind: str | None) -> str:
    """A task of a project capped at $0.4 that spent $0.5 over two steps, failed as its Planner reports."""
    from loopx.db import plans, projects, tasks

    project = await projects.create_project(name="calc", execution={"task_budget_usd": 0.4}, pool=pool)
    await tasks.create_task(task_id="task-a", requirement="build calc", project_id=project["id"],
                            work_dir="/w", pool=pool)
    await plans.save("task-a", steps=[{"id": "step-1", "index": 0, "instruction": "do s1", "status": "verified"},
                                      {"id": "step-2", "index": 1, "instruction": "do s2"}], pool=pool)
    for agent_id, cost in (("planner-task-a", 0.25), ("worker-1", 0.25)):
        await _run_record(pool, agent_id, task_id="task-a", cost=cost)
    router._tasks["task-a"] = Task(task_id="task-a", requirement="build calc", goal_id="", work_dir="/w",
                                   project_id=project["id"], status="running")
    await router._handle_planner_status(MessageEnvelope.create(
        source=planner_address("task-a"), target=TASK_ROUTER_ADDRESS, message_type=MessageType.GOAL_STATUS,
        payload={"task_id": "task-a", "status": "failed", "summary": CAP_REASON, "fail_kind": fail_kind},
    ))
    return project["id"]


async def _call(server: AgentHTTPServer, method: str, path: str, body: Any = None) -> tuple[int, Any]:
    writer = _Writer()
    await server._route_api(method, path, {}, json.dumps(body).encode() if body is not None else b"", writer)
    head, _, payload = writer.data.partition(b"\r\n\r\n")
    return int(head.split()[1]), json.loads(payload)


def _setup() -> tuple[AgentHTTPServer, TaskRouter, list[MessageEnvelope]]:
    published: list[MessageEnvelope] = []
    router = TaskRouter(runtime_root=Path(tempfile.mkdtemp()))
    router._bus = SimpleNamespace(publish=AsyncMock())  # type: ignore[assignment]
    server = AgentHTTPServer(web_adapter=SimpleNamespace(_address=channel_address("web")),
                             supervisor=SimpleNamespace(bus=SimpleNamespace(publish=AsyncMock(
                                 side_effect=published.append))),
                             task_router=router)
    return server, router, published


def test_a_task_that_failed_at_its_cap_continues_from_its_plan_with_a_higher_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from loopx.db import projects, steps, tasks

    async def test(pool: Any) -> None:
        server, router, published = _setup()
        project_id = await _failed_task(pool, router, "task_budget")

        status, detail = await _call(server, "GET", "/api/tasks/task-a")
        assert status == 200
        assert (detail["status"], detail["fail_kind"], detail["task_budget_usd"]) == ("failed", "task_budget", 0.4)
        assert detail["cost"]["cost_usd"] == pytest.approx(0.5)

        status, body = await _call(server, "POST", "/api/tasks/task-a/continue", {"task_budget_usd": 1.25})
        assert status == 200, body
        assert (body["task_budget_usd"], body["spent_usd"], body["continued"]) == (1.25, 0.5, "task-a")
        assert (await projects.get_project(project_id, pool=pool))["task_budget_usd"] == 1.25
        (restart,) = published
        assert restart.message_type is MessageType.TASK_RESTART
        assert restart.payload == {"task_id": "task-a", "resume": True}

        # The router runs it again from its plan: the steps stay, the failure is cleared.
        assign = AsyncMock()
        monkeypatch.setattr(router, "_assign_task", assign)
        del router._tasks["task-a"]
        await router._handle_restart(restart)
        _, request = assign.await_args.args
        assert request.payload["resume"] is True
        row = await tasks.get_task("task-a", pool=pool)
        assert (row["status"], row["error"], row["fail_kind"]) == ("pending", None, None)
        assert [s["id"] for s in await steps.list_steps("task-a", pool=pool)] == ["step-1", "step-2"]

    _run(test, monkeypatch)


@pytest.mark.parametrize("fail_kind, body, error", [
    (None, {"task_budget_usd": 2}, "only a task that failed at its spend cap"),  # it failed for another reason
    ("task_budget", {"task_budget_usd": 0.5}, "above the $0.5 the task has spent"),  # equal to the spend
    ("task_budget", {"task_budget_usd": 0.45}, "above the $0.5 the task has spent"),
    ("task_budget", {}, "task_budget_usd is required"),
    ("task_budget", {"task_budget_usd": "2"}, "number of US dollars"),
])
def test_continuing_is_refused_unless_the_task_failed_at_its_cap_and_the_cap_is_above_its_spend(
    monkeypatch: pytest.MonkeyPatch, fail_kind: str | None, body: dict[str, Any], error: str,
) -> None:
    from loopx.db import projects

    async def test(pool: Any) -> None:
        server, router, published = _setup()
        project_id = await _failed_task(pool, router, fail_kind)

        status, answer = await _call(server, "POST", "/api/tasks/task-a/continue", body)
        assert status == 400 and error in answer["error"]
        assert (await projects.get_project(project_id, pool=pool))["task_budget_usd"] == 0.4
        assert published == []

    _run(test, monkeypatch)
