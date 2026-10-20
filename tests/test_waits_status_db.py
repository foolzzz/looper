"""The task status ``waiting`` counts as in progress on a real PostgreSQL.

It groups with ``running``, the activity query finds it, and a delete is refused.
An internal restart carries it on the way it carries a running task.
``waiting_user`` stays needs-you. Runs only when LOOPER_TEST_DATABASE_URL points
at a disposable database.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from looper.agents.message import TASK_ROUTER_ADDRESS, MessageEnvelope, MessageType
from tests.test_ops_tasks_db import _project, _task
from tests.test_usage_api_db import DSN, _Writer, _run

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")


def test_waiting_is_in_progress_found_active_and_kept(monkeypatch: pytest.MonkeyPatch) -> None:
    """``waiting`` is in progress and active, like ``running``: the activity query finds it and delete is refused."""
    from looper.agents.http_server import AgentHTTPServer
    from looper.agents.tiers.task import TaskRouter
    from looper.db import tasks

    async def test(pool: Any) -> None:
        assert tasks.task_group("waiting") is tasks.TaskGroup.IN_PROGRESS
        assert tasks.task_group("running") is tasks.TaskGroup.IN_PROGRESS
        assert tasks.task_group("waiting_user") is tasks.TaskGroup.NEEDS_YOU
        assert "waiting" in tasks.ACTIVE_STATUSES
        assert "running" in tasks.ACTIVE_STATUSES

        project = await _project(pool)
        await _task(pool, "task-run", "running", project)
        await _task(pool, "task-wait", "waiting", project)
        # The other task in the directory is active, for either one.
        assert await tasks.others_active_in("/w", "task-wait", pool=pool) is True
        assert await tasks.others_active_in("/w", "task-run", pool=pool) is True
        assert {row["id"] for row in await tasks.active_workspaces(pool=pool)} == {"task-run", "task-wait"}

        server = AgentHTTPServer(
            web_adapter=SimpleNamespace(healthcheck=lambda: {}),
            supervisor=SimpleNamespace(agent_status=lambda: {}),
            task_router=TaskRouter(runtime_root=Path(tempfile.mkdtemp())),
        )
        for task_id in ("task-run", "task-wait"):
            writer = _Writer()
            await server._route_api("POST", "/api/tasks/delete", {},
                                    json.dumps({"task_id": task_id}).encode(), writer)
            assert writer.data.startswith(b"HTTP/1.1 400") and b"stop the task before deleting" in writer.data
        assert await pool.fetchval("SELECT count(*) FROM tasks") == 2

    _run(test, monkeypatch)


def test_an_internal_restart_carries_on_a_waiting_task_as_it_does_a_running_one(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """The router's restart path treats ``waiting`` as a running task, the way it treats ``running``."""
    from looper.agents.tiers.task import TaskRouter

    async def test(pool: Any) -> None:
        project = await _project(pool)
        await _task(pool, "task-run", "running", project)
        await _task(pool, "task-wait", "waiting", project)
        router = TaskRouter(runtime_root=tmp_path)
        router._bus = SimpleNamespace()  # type: ignore[assignment]
        assigned: list[tuple[str, str]] = []

        async def assign(task: Any, envelope: MessageEnvelope) -> None:
            assigned.append((task.task_id, envelope.payload["ended"]))

        router._assign_task = assign  # type: ignore[method-assign]
        for task_id in ("task-run", "task-wait"):
            await router._handle_restart(MessageEnvelope.create(
                source=TASK_ROUTER_ADDRESS, target=TASK_ROUTER_ADDRESS, message_type=MessageType.TASK_RESTART,
                payload={"task_id": task_id}))
        assert assigned == [("task-run", "running"), ("task-wait", "waiting")]
        assert {row["id"]: row["status"] for row in await pool.fetch("SELECT id, status FROM tasks")} == {
            "task-run": "pending", "task-wait": "pending"}

    _run(test, monkeypatch)
