"""A failing cleanup step must not drop a task stop: ``stopped`` is always written."""
from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from looper.agents import cost_guard
from looper.agents.bus import MessageBus
from looper.agents.message import TASK_ROUTER_ADDRESS, MessageEnvelope, MessageType, channel_address
from looper.agents.tiers.task import Task, TaskRouter
from looper.db import projects as db_projects, tasks
from tests.db_support import DSN, run_db

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")
SOURCE = channel_address("web")
FAILING_STEPS = ["clear_failure_holds", "release_queued", "cancel_publish"]


def _stop(task_id):
    return MessageEnvelope.create(source=SOURCE, target=TASK_ROUTER_ADDRESS, message_type=MessageType.TASK_STOP,
                                  payload={"task_id": task_id})


async def _setup(pool, tmp_path, monkeypatch, failing):
    project = await db_projects.create_project(name="Example", pool=pool)
    await tasks.create_task(task_id="task-stop", requirement="Build", project_id=project["id"], work_dir="",
                                  status="waiting_user", manager_id="manager", pool=pool)
    row = await tasks.get_task("task-stop", pool=pool)
    ended = []

    async def on_end(task_id, status):
        ended.append((task_id, status))

    bus = MessageBus(runtime_root=tmp_path)
    router = TaskRouter(runtime_root=tmp_path, on_task_end=on_end)
    router._bus = bus
    router._tasks["task-stop"] = Task.from_row("task-stop", row)
    planner = SimpleNamespace(agent_address=channel_address("planner-stub"), stop=AsyncMock())
    router._planners["task-stop"] = planner

    async def boom(*args, **kwargs):
        raise RuntimeError("db unavailable")

    if failing == "clear_failure_holds":
        monkeypatch.setattr(TaskRouter, "_clear_failure_holds", boom)
    elif failing == "release_queued":
        monkeypatch.setattr(cost_guard, "release_queued", boom)
    else:
        monkeypatch.setattr(bus, "publish", boom)
    return bus, router, planner, ended


@pytest.mark.parametrize("failing", FAILING_STEPS)
def test_stop_completes_when_a_cleanup_step_fails(monkeypatch, tmp_path, caplog, failing):
    async def run(pool):
        _bus, router, planner, ended = await _setup(pool, tmp_path, monkeypatch, failing)
        with caplog.at_level(logging.WARNING, logger="looper.agents.tiers.task"):
            await router._handle(_stop("task-stop"))
        assert (await tasks.get_task("task-stop"))["status"] == "stopped"
        assert "task-stop" not in router._planners
        planner.stop.assert_awaited_once()
        assert ended == [("task-stop", "stopped")]
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING and r.exc_info]
        # release_queued also runs when the task ends, so it fails (and is logged) once more there.
        assert len(warnings) == (2 if failing == "release_queued" else 1)
        assert all(failing in r.getMessage() for r in warnings)

    run_db(run, monkeypatch, timeout=20)


def test_stop_through_the_message_loop_is_consumed_once(monkeypatch, tmp_path):
    async def run(pool):
        bus, router, planner, ended = await _setup(pool, tmp_path, monkeypatch, "clear_failure_holds")
        calls = []
        original = router._handle_stop

        async def counted(envelope):
            calls.append(envelope.message_id)
            await original(envelope)

        monkeypatch.setattr(router, "_handle_stop", counted)
        await router.start(bus)
        try:
            await bus.publish(_stop("task-stop"))
            for _ in range(200):
                if ended:
                    break
                await asyncio.sleep(0.05)
            assert ended == [("task-stop", "stopped")]
            assert (await tasks.get_task("task-stop"))["status"] == "stopped"
            assert len(calls) == 1
        finally:
            await router.stop()
            await bus.shutdown()

    run_db(run, monkeypatch, timeout=20)
