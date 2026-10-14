"""Tests for TaskRouter: per-task Planner lifecycle management."""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest

from looper.agents.bus import MessageBus
from looper.agents.message import (
    MANAGER_ADDRESS,
    TASK_ROUTER_ADDRESS,
    MessageEnvelope,
    MessageType,
    planner_address,
)
from looper.agents.tiers.task import Task, TaskRouter


async def _until(condition: Any, timeout: float = 5.0) -> None:
    """Wait until ``condition()`` holds, checking every 10 ms."""
    deadline = asyncio.get_running_loop().time() + timeout
    while not condition():
        assert asyncio.get_running_loop().time() < deadline, "timed out waiting for the router"
        await asyncio.sleep(0.01)


@pytest.mark.usefixtures("agent_db")
class TestTaskRouter:
    def test_task_cancel(self) -> None:
        async def _run() -> None:
            with tempfile.TemporaryDirectory() as tmp:
                bus = MessageBus(runtime_root=Path(tmp))
                router = TaskRouter(runtime_root=Path(tmp))
                await router.start(bus)

                envelope = MessageEnvelope.create(
                    source=MANAGER_ADDRESS,
                    target=TASK_ROUTER_ADDRESS,
                    message_type=MessageType.TASK_REQUEST,
                    payload={
                        "goal_id": "g1",
                        "requirement": "some work",
                        "work_dir": "/tmp",
                        "project_id": "proj-1",
                    },
                    metadata={"origin_channel_id": "channel-web"},
                )
                await bus.publish(envelope)
                await _until(lambda: len(router._planners) == 1)

                task_id = list(router._tasks.keys())[0]

                cancel_env = MessageEnvelope.create(
                    source=MANAGER_ADDRESS,
                    target=TASK_ROUTER_ADDRESS,
                    message_type=MessageType.TASK_CANCEL,
                    payload={"task_id": task_id},
                )
                await bus.publish(cancel_env)
                await _until(lambda: task_id not in router._planners)

                assert router._tasks[task_id].status == "cancelled"
                assert task_id not in router._planners

                await router.stop()
                await bus.shutdown()

        asyncio.run(_run())

    def test_exceeding_concurrency_queues_task(self) -> None:
        async def _run() -> None:
            with tempfile.TemporaryDirectory() as tmp:
                bus = MessageBus(runtime_root=Path(tmp))
                router = TaskRouter(runtime_root=Path(tmp))
                await router.start(bus)

                # Send 4 tasks (max concurrent = 3)
                for i in range(4):
                    envelope = MessageEnvelope.create(
                        source=MANAGER_ADDRESS,
                        target=TASK_ROUTER_ADDRESS,
                        message_type=MessageType.TASK_REQUEST,
                        payload={
                            "goal_id": f"g{i}",
                            "requirement": f"task {i}",
                            "work_dir": "/tmp",
                            "project_id": "proj-1",
                        },
                        metadata={"origin_channel_id": "channel-web"},
                    )
                    await bus.publish(envelope)

                # Every request has been placed: given a Planner or queued.
                await _until(lambda: len(router._tasks) == 4
                             and len(router._planners) + router._pending_queue.qsize() == 4)

                running = sum(1 for t in router._tasks.values() if t.status == "running")
                pending = sum(1 for t in router._tasks.values() if t.status == "pending")
                assert running == 3
                assert pending == 1
                assert len(router._planners) == 3

                await router.stop()
                await bus.shutdown()

        asyncio.run(_run())

    def test_forwards_status_to_manager(self, planner_claude: Any) -> None:
        from tests.test_planner_agent import plan, step

        claude = planner_claude(plan(step("s1")))  # the Planner's Claude plans one step

        async def _run() -> None:
            with tempfile.TemporaryDirectory() as tmp:
                bus = MessageBus(runtime_root=Path(tmp))
                router = TaskRouter(runtime_root=Path(tmp), claude_bin=claude.bin)
                await router.start(bus)

                envelope = MessageEnvelope.create(
                    source=MANAGER_ADDRESS,
                    target=TASK_ROUTER_ADDRESS,
                    message_type=MessageType.TASK_REQUEST,
                    payload={
                        "goal_id": "g1",
                        "requirement": "progress test",
                        "work_dir": "/tmp",
                        "project_id": "proj-1",
                    },
                    metadata={"origin_channel_id": "channel-web"},
                )
                await bus.publish(envelope)
                manager_q = bus.get_queue("manager")
                for _ in range(500):
                    if manager_q.qsize() >= 2:
                        break
                    await asyncio.sleep(0.01)

                # Collect messages forwarded to manager
                messages: list[MessageEnvelope] = []
                while not manager_q.empty():
                    messages.append(await manager_q.get())

                # TaskRouter should have forwarded TASK_ACCEPTED + GOAL_STATUS
                types = [m.message_type for m in messages]
                assert MessageType.TASK_ACCEPTED in types and MessageType.GOAL_STATUS in types

                await router.stop()
                await bus.shutdown()

        asyncio.run(_run())


class TestPlannerOutcome:
    @pytest.mark.parametrize(
        ("outcome", "fields"),
        [
            ("completed", {"result_summary": "calc is built", "error": None}),
            ("failed", {"result_summary": None, "error": "calc is built"}),
            ("executing", {"result_summary": None, "error": None}),
        ],
    )
    def test_final_summary_is_the_tasks_result_or_error(
        self, outcome: str, fields: dict[str, Any], agent_db: Any,
    ) -> None:
        async def _run() -> None:
            with tempfile.TemporaryDirectory() as tmp:
                bus = MessageBus(runtime_root=Path(tmp))
                router = TaskRouter(runtime_root=Path(tmp))
                router._bus = bus
                router._tasks = {"t1": Task(task_id="t1", requirement="r", goal_id="", work_dir="",
                                            project_id="proj-1", status="running")}
                await router._handle_planner_status(MessageEnvelope.create(
                    source=planner_address("t1"), target=TASK_ROUTER_ADDRESS,
                    message_type=MessageType.GOAL_STATUS,
                    payload={"task_id": "t1", "status": outcome, "summary": "calc is built"},
                ))
                await bus.shutdown()

        asyncio.run(_run())
        first = agent_db.update_task_status.await_args_list[0]
        assert first.args == ("t1", outcome)
        assert {key: first.kwargs.get(key) for key in fields} == fields
        # Teardown persists the Planner's outcome; it never turns a failed task into a completed one.
        assert agent_db.update_task_status.await_args_list[-1].args == ("t1", outcome)


@pytest.mark.usefixtures("agent_db")
class TestTaskProject:
    def _request(self, **payload: Any) -> MessageEnvelope:
        return MessageEnvelope.create(
            source=MANAGER_ADDRESS,
            target=TASK_ROUTER_ADDRESS,
            message_type=MessageType.TASK_REQUEST,
            payload={"goal_id": "g1", "requirement": "build it", "work_dir": "/tmp", **payload},
            metadata={"origin_channel_id": "channel-web"},
        )

    def test_task_keeps_its_project(self, agent_db: Any) -> None:
        async def _run() -> Task:
            with tempfile.TemporaryDirectory() as tmp:
                bus = MessageBus(runtime_root=Path(tmp))
                router = TaskRouter(runtime_root=Path(tmp))
                router._bus = bus
                router._assign_task = AsyncMock()  # type: ignore[method-assign]
                await router._handle_task_request(self._request(project_id="proj-1"))
                await bus.shutdown()
                (task,) = router._tasks.values()
                return task

        task = asyncio.run(_run())
        assert task.project_id == "proj-1"
        assert agent_db.create_task.await_args.kwargs["project_id"] == "proj-1"

    def test_request_without_a_project_is_dropped(
        self, agent_db: Any, caplog: pytest.LogCaptureFixture,
    ) -> None:
        """A request from before projects existed, replayed from the bus journal, creates nothing."""

        async def _run() -> TaskRouter:
            with tempfile.TemporaryDirectory() as tmp:
                bus = MessageBus(runtime_root=Path(tmp))
                router = TaskRouter(runtime_root=Path(tmp))
                router._bus = bus
                router._assign_task = AsyncMock()  # type: ignore[method-assign]
                await router._handle_task_request(self._request())
                await bus.shutdown()
                return router

        with caplog.at_level("ERROR", logger="looper.agents.tiers.task"):
            router = asyncio.run(_run())
        assert router._tasks == {}
        router._assign_task.assert_not_awaited()  # type: ignore[attr-defined]
        agent_db.create_task.assert_not_awaited()
        assert any("project" in record.getMessage() for record in caplog.records)

    def test_restart_keeps_the_project(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from looper.db import tasks as db_tasks

        row = {"id": "t1", "requirement": "build it", "goal_id": "g1", "work_dir": "/tmp/p",
               "project_id": "proj-7", "status": "stopped", "correlation_id": "c1", "origin_channel_id": ""}
        monkeypatch.setattr(db_tasks, "get_task", AsyncMock(return_value=row))
        monkeypatch.setattr(db_tasks, "reset_task_for_restart", AsyncMock(return_value=True))

        async def _run() -> tuple[Task, MessageEnvelope]:
            with tempfile.TemporaryDirectory() as tmp:
                bus = MessageBus(runtime_root=Path(tmp))
                router = TaskRouter(runtime_root=Path(tmp))
                router._bus = bus
                assign = AsyncMock()
                router._assign_task = assign  # type: ignore[method-assign]
                await router._handle_restart(MessageEnvelope.create(
                    source=MANAGER_ADDRESS, target=TASK_ROUTER_ADDRESS,
                    message_type=MessageType.TASK_RESTART, payload={"task_id": "t1"},
                ))
                await bus.shutdown()
                task, envelope = assign.await_args.args
                return task, envelope

        task, envelope = asyncio.run(_run())
        assert task.project_id == "proj-7"
        assert envelope.payload["project_id"] == "proj-7"

