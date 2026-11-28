"""A stopped task stays stopped when a late Planner status reaches the router."""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from looper.agents.bus import MessageBus
from looper.agents.message import TASK_ROUTER_ADDRESS, MessageEnvelope, MessageType, channel_address
from looper.agents.tiers.task import Task, TaskRouter
from looper.db import projects as db_projects, tasks
from tests.db_support import DSN, run_db

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")
SOURCE = channel_address("web")
PLANNER = channel_address("planner-stub")


def _stop(task_id):
    return MessageEnvelope.create(source=SOURCE, target=TASK_ROUTER_ADDRESS, message_type=MessageType.TASK_STOP,
                                  payload={"task_id": task_id})


def _status(status):
    return MessageEnvelope.create(source=PLANNER, target=TASK_ROUTER_ADDRESS, message_type=MessageType.GOAL_STATUS,
                                  payload={"task_id": "task-stop", "status": status, "summary": "late"})


async def _setup(pool, tmp_path, db_status="waiting_user"):
    project = await db_projects.create_project(name="Example", pool=pool)
    await tasks.create_task(task_id="task-stop", requirement="Build", project_id=project["id"], work_dir="",
                            status=db_status, manager_id="manager", pool=pool)
    row = await tasks.get_task("task-stop", pool=pool)
    ended = []

    async def on_end(task_id, status):
        ended.append((task_id, status))

    router = TaskRouter(runtime_root=tmp_path, on_task_end=on_end)
    router._bus = MessageBus(runtime_root=tmp_path)
    router._tasks["task-stop"] = Task.from_row("task-stop", row)
    router._planners["task-stop"] = SimpleNamespace(agent_address=PLANNER, stop=AsyncMock())
    return router, ended


def test_a_late_planner_status_does_not_overwrite_a_stop(monkeypatch, tmp_path):
    async def run(pool):
        router, ended = await _setup(pool, tmp_path)
        await router._handle(_stop("task-stop"))
        assert ended == [("task-stop", "stopped")]
        published = []
        monkeypatch.setattr(router._bus, "publish", AsyncMock(side_effect=published.append))
        for status in ("executing", "completed", "failed"):
            await router._handle(_status(status))
            assert (await tasks.get_task("task-stop"))["status"] == "stopped"
        assert router._tasks["task-stop"].status == "stopped"
        assert ended == [("task-stop", "stopped")]
        assert published == []

    run_db(run, monkeypatch, timeout=20)


@pytest.mark.parametrize("ended_status", ["stopped", "cancelled"])
def test_the_row_refuses_a_planner_status_once_it_is_stopped_or_cancelled(monkeypatch, tmp_path, ended_status):
    async def run(pool):
        # The router's memory still says running: only the row knows the task has ended.
        router, ended = await _setup(pool, tmp_path, db_status=ended_status)
        router._tasks["task-stop"].status = "running"
        published = []
        monkeypatch.setattr(router._bus, "publish", AsyncMock(side_effect=published.append))
        for status in ("executing", "completed"):
            await router._handle(_status(status))
            assert (await tasks.get_task("task-stop"))["status"] == ended_status
        assert ended == []
        assert published == []
        assert router._tasks["task-stop"].status == ended_status

    run_db(run, monkeypatch, timeout=20)


def test_a_task_restarts_through_the_normal_path_after_a_stop(monkeypatch, tmp_path):
    async def run(pool):
        router, _ended = await _setup(pool, tmp_path)
        await router._handle(_stop("task-stop"))
        assert (await tasks.get_task("task-stop"))["status"] == "stopped"
        monkeypatch.setattr(router, "_assign_task", AsyncMock())
        router._tasks.pop("task-stop")
        await router._handle_restart(MessageEnvelope.create(
            source=SOURCE, target=TASK_ROUTER_ADDRESS, message_type=MessageType.TASK_RESTART,
            payload={"task_id": "task-stop", "mode": "continue"}))
        assert (await tasks.get_task("task-stop"))["status"] == "pending"
        # The restarted task takes the Planner's status again.
        router._tasks["task-stop"].status = "running"
        await router._handle(_status("executing"))
        assert (await tasks.get_task("task-stop"))["status"] == "executing"

    run_db(run, monkeypatch, timeout=20)
