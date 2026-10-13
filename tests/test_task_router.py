"""Tests for TaskRouter: per-task Planner lifecycle management."""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest

from loopx.agents.bus import MessageBus
from loopx.agents.message import (
    MANAGER_ADDRESS,
    TASK_ROUTER_ADDRESS,
    MessageEnvelope,
    MessageType,
    planner_address,
    worker_address,
)
from loopx.agents.tiers.task import Task, TaskRouter


class TestTask:
    def test_task_to_dict(self) -> None:
        task = Task(
            task_id="t1",
            requirement="add login",
            goal_id="g1",
            work_dir="/tmp",
            project_id="proj-1",
            status="running",
            planner_id="planner-t1",
            correlation_id="c1",
        )
        d = task.to_dict()
        assert d["task_id"] == "t1"
        assert d["status"] == "running"
        assert d["planner_id"] == "planner-t1"

    def test_task_default_status(self) -> None:
        task = Task(
            task_id="t1",
            requirement="test",
            goal_id="g1",
            work_dir="/tmp",
            project_id="proj-1",
        )
        assert task.status == "pending"


@pytest.mark.usefixtures("agent_db")
class TestTaskRouter:
    def test_agent_id_and_address(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            router = TaskRouter(runtime_root=Path(tmp))
            assert router.agent_id == "task-router"
            assert router.agent_address == TASK_ROUTER_ADDRESS

    def test_create_planner_on_task_request(self) -> None:
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
                        "requirement": "add login page",
                        "work_dir": "/tmp",
                        "project_id": "proj-1",
                    },
                    metadata={"origin_channel_id": "channel-web"},
                )
                await bus.publish(envelope)
                await asyncio.sleep(0.2)

                assert len(router._tasks) == 1
                assert len(router._planners) == 1
                task = list(router._tasks.values())[0]
                assert task.status == "running"
                assert task.planner_id is not None

                await router.stop()
                await bus.shutdown()

        asyncio.run(_run())

    def test_multiple_tasks_independent(self) -> None:
        async def _run() -> None:
            with tempfile.TemporaryDirectory() as tmp:
                bus = MessageBus(runtime_root=Path(tmp))
                router = TaskRouter(runtime_root=Path(tmp))
                await router.start(bus)

                for i in range(3):
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

                await asyncio.sleep(0.3)

                assert len(router._tasks) == 3
                assert len(router._planners) == 3

                statuses = [t.status for t in router._tasks.values()]
                assert all(s == "running" for s in statuses)

                await router.stop()
                await bus.shutdown()

        asyncio.run(_run())

    def test_task_completion_tears_down_planner(self) -> None:
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
                        "requirement": "quick fix",
                        "work_dir": "/tmp",
                        "project_id": "proj-1",
                    },
                    metadata={"origin_channel_id": "channel-web"},
                )
                await bus.publish(envelope)
                await asyncio.sleep(0.2)

                task_id = list(router._tasks.keys())[0]
                planner = router._planners[task_id]

                # Simulate planner reporting completion
                status_env = MessageEnvelope.create(
                    source=planner.agent_address,
                    target=TASK_ROUTER_ADDRESS,
                    message_type=MessageType.GOAL_STATUS,
                    payload={
                        "task_id": task_id,
                        "goal_id": "g1",
                        "status": "completed",
                        "summary": "done",
                    },
                    correlation_id=envelope.correlation_id,
                )
                await bus.publish(status_env)
                await asyncio.sleep(0.2)

                assert task_id not in router._planners

                await router.stop()
                await bus.shutdown()

        asyncio.run(_run())

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
                await asyncio.sleep(0.2)

                task_id = list(router._tasks.keys())[0]

                cancel_env = MessageEnvelope.create(
                    source=MANAGER_ADDRESS,
                    target=TASK_ROUTER_ADDRESS,
                    message_type=MessageType.TASK_CANCEL,
                    payload={"task_id": task_id},
                )
                await bus.publish(cancel_env)
                await asyncio.sleep(0.2)

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

                await asyncio.sleep(0.3)

                running = sum(1 for t in router._tasks.values() if t.status == "running")
                pending = sum(1 for t in router._tasks.values() if t.status == "pending")
                assert running == 3
                assert pending == 1
                assert len(router._planners) == 3

                await router.stop()
                await bus.shutdown()

        asyncio.run(_run())

    def test_list_tasks(self) -> None:
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
                        "requirement": "test list",
                        "work_dir": "/tmp",
                        "project_id": "proj-1",
                    },
                    metadata={"origin_channel_id": "channel-web"},
                )
                await bus.publish(envelope)
                await asyncio.sleep(0.2)

                tasks = router.list_tasks()
                assert len(tasks) == 1
                assert tasks[0]["requirement"] == "test list"
                assert tasks[0]["status"] == "running"

                await router.stop()
                await bus.shutdown()

        asyncio.run(_run())

    def test_forwards_status_to_manager(self) -> None:
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
                        "requirement": "progress test",
                        "work_dir": "/tmp",
                        "project_id": "proj-1",
                    },
                    metadata={"origin_channel_id": "channel-web"},
                )
                await bus.publish(envelope)
                await asyncio.sleep(0.2)

                # Collect messages forwarded to manager
                manager_q = bus.get_queue("manager")
                messages: list[MessageEnvelope] = []
                while not manager_q.empty():
                    messages.append(await manager_q.get())

                # TaskRouter should have forwarded TASK_ACCEPTED + GOAL_STATUS
                types = [m.message_type for m in messages]
                assert MessageType.TASK_ACCEPTED in types or MessageType.GOAL_STATUS in types

                await router.stop()
                await bus.shutdown()

        asyncio.run(_run())


class TestPlannerOutcome:
    @pytest.mark.parametrize("outcome", ["completed", "failed"])
    def test_final_status_is_kept(self, outcome: str, agent_db: Any) -> None:
        """Teardown must persist the Planner's outcome, not overwrite failed with completed."""

        async def _run() -> Task:
            with tempfile.TemporaryDirectory() as tmp:
                bus = MessageBus(runtime_root=Path(tmp))
                router = TaskRouter(runtime_root=Path(tmp))
                router._bus = bus
                task = Task(task_id="t1", requirement="r", goal_id="", work_dir="", project_id="proj-1", status="running")
                router._tasks = {"t1": task}
                await router._handle_planner_status(MessageEnvelope.create(
                    source=planner_address("t1"),
                    target=TASK_ROUTER_ADDRESS,
                    message_type=MessageType.GOAL_STATUS,
                    payload={"task_id": "t1", "status": outcome},
                ))
                await bus.shutdown()
                return task

        task = asyncio.run(_run())
        assert task.status == outcome
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

        with caplog.at_level("ERROR", logger="loopx.agents.tiers.task"):
            router = asyncio.run(_run())
        assert router._tasks == {}
        router._assign_task.assert_not_awaited()  # type: ignore[attr-defined]
        agent_db.create_task.assert_not_awaited()
        assert any("project" in record.getMessage() for record in caplog.records)

    def test_restart_keeps_the_project(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from loopx.db import tasks as db_tasks

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

    def test_moving_a_task_updates_its_live_entry(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            router = TaskRouter(runtime_root=Path(tmp))
            live = Task(task_id="t1", requirement="r", goal_id="", work_dir="/w", project_id="proj-1", status="running")
            router._tasks = {"t1": live}
            router.set_task_project("t1", "proj-2")
            router.set_task_project("t-unknown", "proj-2")  # not held: nothing to update
            assert live.project_id == "proj-2"
            assert list(router._tasks) == ["t1"]
