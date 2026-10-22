"""A task reports its progress and end to the Manager that created it (``TaskRouter._manager_for``)."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

import pytest

from looper.agents.bus import MessageBus
from looper.agents.message import TASK_ROUTER_ADDRESS, MessageEnvelope, MessageType, manager_address
from looper.agents.tiers.task import Task, TaskRouter


def _task(**kw: Any) -> Task:
    return Task(task_id="task-r1", requirement="r", goal_id="g1", work_dir="/tmp", project_id="p", title="整理",
                **kw)


def _status(status: str = "executing") -> MessageEnvelope:
    return MessageEnvelope.create(
        source=TASK_ROUTER_ADDRESS, target=TASK_ROUTER_ADDRESS, message_type=MessageType.GOAL_STATUS,
        payload={"task_id": "task-r1", "goal_id": "g1", "status": status, "summary": "half way"},
        correlation_id="c1", metadata={"origin_channel_id": "channel-web"})


def _accepted() -> MessageEnvelope:
    return MessageEnvelope.create(
        source=TASK_ROUTER_ADDRESS, target=TASK_ROUTER_ADDRESS, message_type=MessageType.TASK_ACCEPTED,
        payload={"task_id": "task-r1", "step_count": 3}, correlation_id="c1",
        metadata={"origin_channel_id": "channel-web"})


async def _router(tmp_path: Path, task: Task) -> tuple[TaskRouter, MessageBus]:
    bus = MessageBus(runtime_root=tmp_path)
    router = TaskRouter(runtime_root=tmp_path)
    router._bus = bus
    router._tasks[task.task_id] = task
    return router, bus


def test_a_status_goes_to_the_tasks_manager(tmp_path: Path) -> None:
    async def _main() -> None:
        router, bus = await _router(tmp_path, _task(manager_id="manager-review"))
        await router._handle_planner_status(_status())
        assert bus.get_queue("manager-review").qsize() == 1
        assert bus.get_queue("manager").qsize() == 0

    asyncio.run(_main())


def test_a_stopped_manager_falls_back_to_the_default_with_one_warning(
        tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    async def _main() -> None:
        router, bus = await _router(tmp_path, _task(manager_id="manager-review"))
        router.manager_running = lambda mid: mid == "manager"
        with caplog.at_level(logging.WARNING, logger="looper.agents.tiers.task"):
            await router._handle_planner_status(_status())
            await router._handle_planner_status(_status())
        assert bus.get_queue("manager").qsize() == 2
        assert bus.get_queue("manager-review").qsize() == 0
        assert len([r for r in caplog.records if "is not running" in r.getMessage()]) == 1

    asyncio.run(_main())


def test_task_accepted_goes_to_the_tasks_manager(tmp_path: Path) -> None:
    async def _main() -> None:
        router, bus = await _router(tmp_path, _task(manager_id="manager-review"))
        await router._handle_task_accepted(_accepted())
        assert bus.get_queue("manager-review").qsize() == 1
        assert bus.get_queue("manager").qsize() == 0

    asyncio.run(_main())


def test_reports_carry_the_web_thread_and_the_title(tmp_path: Path) -> None:
    async def _main() -> None:
        router, bus = await _router(tmp_path, _task(origin_thread="s-web"))
        await router._handle_task_accepted(_accepted())
        await router._handle_planner_status(_status())
        queue = bus.get_queue("manager")
        for _ in range(2):
            envelope = queue.get_nowait()
            assert envelope.metadata["origin_thread"] == "s-web"
            assert envelope.metadata["task_title"] == "整理"
            assert envelope.metadata["origin_channel_id"] == "channel-web"

    asyncio.run(_main())


def test_from_row_defaults() -> None:
    task = Task.from_row("t", {"requirement": "r", "project_id": "p", "manager_id": None, "origin_thread": None})
    assert (task.manager_id, task.origin_thread) == ("manager", "")
    assert manager_address("manager-review").agent_id == "manager-review"
