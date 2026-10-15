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
                        "task_id": "task-c1",
                        "goal_id": "g1",
                        "requirement": "some work",
                        "work_dir": "/tmp",
                        "project_id": "proj-1",
                    },
                    metadata={"origin_channel_id": "channel-web"},
                )
                await bus.publish(envelope)
                await bus.publish(_start("task-c1"))
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
                        "task_id": "task-p1",
                        "goal_id": "g1",
                        "requirement": "progress test",
                        "work_dir": "/tmp",
                        "project_id": "proj-1",
                    },
                    metadata={"origin_channel_id": "channel-web"},
                )
                await bus.publish(envelope)
                await bus.publish(_start("task-p1"))
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


def _planner_limits(agent_db: Any, *, role: int | None = None, projects: dict[str, int] | None = None) -> None:
    """Store the Planner's concurrency limit on its role layer and on its layer in some projects."""
    rows = [{"scope": "role", "role": "planner", "project_id": None, "max_concurrency": role}] if role else []
    rows += [{"scope": "role_project", "role": "planner", "project_id": project_id, "max_concurrency": limit}
             for project_id, limit in (projects or {}).items()]
    agent_db.list_layers.return_value = rows


def _request(project_id: str, requirement: str) -> MessageEnvelope:
    """The Manager asking for a task, named after its requirement (``task-<requirement>``)."""
    return MessageEnvelope.create(
        source=MANAGER_ADDRESS, target=TASK_ROUTER_ADDRESS, message_type=MessageType.TASK_REQUEST,
        payload={"task_id": f"task-{requirement}", "goal_id": "g", "requirement": requirement, "work_dir": "/tmp",
                 "project_id": project_id},
        metadata={"origin_channel_id": "channel-web"},
    )


def _start(task_id: str) -> MessageEnvelope:
    """A user starting a ready task (the dashboard's Start, or the Manager's ``task_start``)."""
    return MessageEnvelope.create(source=MANAGER_ADDRESS, target=TASK_ROUTER_ADDRESS,
                                  message_type=MessageType.TASK_START, payload={"task_id": task_id})


async def _ask_and_start(bus: MessageBus, project_id: str, requirement: str) -> None:
    """Ask for a task, then start it."""
    await bus.publish(_request(project_id, requirement))
    await bus.publish(_start(f"task-{requirement}"))


def _ended(task_id: str) -> MessageEnvelope:
    """The task's Planner reporting it completed."""
    return MessageEnvelope.create(
        source=planner_address(task_id), target=TASK_ROUTER_ADDRESS, message_type=MessageType.GOAL_STATUS,
        payload={"task_id": task_id, "status": "completed", "summary": "done"},
    )


class TestProjectConcurrency:
    """At most the Planner's limit of a project's tasks run at once; the rest wait, oldest first, and never hold
    up another project's."""

    @staticmethod
    def _statuses(router: TaskRouter) -> dict[str, str]:
        return {task.requirement: task.status for task in router._tasks.values()}

    @staticmethod
    def _id(router: TaskRouter, requirement: str) -> str:
        return next(task.task_id for task in router._tasks.values() if task.requirement == requirement)

    @staticmethod
    def _placed(router: TaskRouter, count: int) -> Any:
        return lambda: len(router._tasks) == count and len(router._planners) + len(router._pending) == count

    def test_a_project_at_its_limit_queues_its_tasks_without_delaying_another(self, agent_db: Any) -> None:
        _planner_limits(agent_db, role=2)

        async def _run() -> tuple[dict[str, str], dict[str, str]]:
            with tempfile.TemporaryDirectory() as tmp:
                bus = MessageBus(runtime_root=Path(tmp))
                router = TaskRouter(runtime_root=Path(tmp))
                await router.start(bus)
                for requirement in ("a1", "a2", "a3", "a4"):
                    await _ask_and_start(bus, "proj-a", requirement)
                await _ask_and_start(bus, "proj-b", "b1")
                await _until(self._placed(router, 5))
                before = self._statuses(router)

                await bus.publish(_ended(self._id(router, "a1")))
                await _until(lambda: self._statuses(router)["a3"] == "running")
                after = self._statuses(router)
                await router.stop()
                await bus.shutdown()
                return before, after

        before, after = asyncio.run(_run())
        assert before == {"a1": "running", "a2": "running", "a3": "pending", "a4": "pending", "b1": "running"}
        # The oldest waiting task of the project takes the place its ended task left.
        assert after == {**before, "a1": "completed", "a3": "running"}

    def test_a_raised_limit_starts_waiting_tasks_and_a_lowered_one_stops_none(self, agent_db: Any) -> None:
        _planner_limits(agent_db, role=3, projects={"proj-a": 1})

        async def _run() -> list[dict[str, str]]:
            with tempfile.TemporaryDirectory() as tmp:
                bus = MessageBus(runtime_root=Path(tmp))
                router = TaskRouter(runtime_root=Path(tmp))
                await router.start(bus)
                for requirement in ("a1", "a2", "a3"):
                    await _ask_and_start(bus, "proj-a", requirement)
                await _until(self._placed(router, 3))
                seen = [self._statuses(router)]

                _planner_limits(agent_db, role=3, projects={"proj-a": 2})
                await router.recheck()
                seen.append(self._statuses(router))

                _planner_limits(agent_db, role=3)  # the project's override cleared: the role's 3
                await router.recheck()
                seen.append(self._statuses(router))

                _planner_limits(agent_db, role=1)
                await router.recheck()
                await _ask_and_start(bus, "proj-a", "a4")
                await _until(self._placed(router, 4))
                seen.append(self._statuses(router))
                await router.stop()
                await bus.shutdown()
                return seen

        first, raised, cleared, lowered = asyncio.run(_run())
        assert first == {"a1": "running", "a2": "pending", "a3": "pending"}
        assert raised == {"a1": "running", "a2": "running", "a3": "pending"}
        assert cleared == {"a1": "running", "a2": "running", "a3": "running"}
        # Lowering a limit stops nothing that runs; a new task waits until fewer run than it.
        assert lowered == {**cleared, "a4": "pending"}

    @pytest.mark.parametrize("arrival", ["start", "restart", "resume"])
    def test_a_started_restarted_or_resumed_task_waits_for_room(
        self, agent_db: Any, monkeypatch: pytest.MonkeyPatch, arrival: str,
    ) -> None:
        from looper.db import tasks as db_tasks

        _planner_limits(agent_db, projects={"proj-a": 1})
        row = {"id": "task-old", "requirement": "a2", "goal_id": "g", "work_dir": "/tmp", "project_id": "proj-a",
               "status": "stopped" if arrival == "restart" else "executing", "correlation_id": "c-old",
               "origin_channel_id": ""}
        monkeypatch.setattr(db_tasks, "get_task", AsyncMock(return_value=row))
        monkeypatch.setattr(db_tasks, "reset_task_for_restart", AsyncMock(return_value=True))

        async def _run() -> tuple[list[str], str, list[str]]:
            with tempfile.TemporaryDirectory() as tmp:
                bus = MessageBus(runtime_root=Path(tmp))
                router = TaskRouter(runtime_root=Path(tmp))
                assigned: list[str] = []
                assign = router._assign_task

                async def recording(task: Task, envelope: MessageEnvelope) -> None:
                    assigned.append(task.requirement)
                    await assign(task, envelope)

                router._assign_task = recording  # type: ignore[method-assign]
                await router.start(bus)
                await _ask_and_start(bus, "proj-a", "a1")
                await _until(self._placed(router, 1))
                if arrival == "start":
                    await _ask_and_start(bus, "proj-a", "a2")
                elif arrival == "restart":
                    await bus.publish(MessageEnvelope.create(
                        source=MANAGER_ADDRESS, target=TASK_ROUTER_ADDRESS, message_type=MessageType.TASK_RESTART,
                        payload={"task_id": "task-old"}))
                else:
                    await router.resume_tasks(["task-old"])
                await _until(self._placed(router, 2))
                waiting, status = list(assigned), self._statuses(router)["a2"]

                await bus.publish(_ended(self._id(router, "a1")))
                await _until(lambda: len(assigned) == 2)
                await router.stop()
                await bus.shutdown()
                return waiting, status, assigned

        waiting, status, assigned = asyncio.run(_run())
        assert (waiting, status) == (["a1"], "pending")
        assert assigned == ["a1", "a2"]


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

    def test_a_request_creates_the_task_ready_and_only_a_start_runs_it_once(self, agent_db: Any) -> None:
        async def _run() -> tuple[dict[str, Task], list[Any]]:
            with tempfile.TemporaryDirectory() as tmp:
                bus = MessageBus(runtime_root=Path(tmp))
                router = TaskRouter(runtime_root=Path(tmp))
                router._bus = bus
                assign = AsyncMock()
                router._assign_task = assign  # type: ignore[method-assign]
                await router._handle_task_request(self._request(project_id="proj-1", task_id="task-r1"))
                waiting = dict(router._tasks)
                assign.assert_not_awaited()  # no Planner until a user starts it
                for _ in range(2):  # starting it again does nothing
                    await router._handle_start(_start("task-r1"))
                await bus.shutdown()
                return waiting, [call.args for call in assign.await_args_list]

        waiting, assigned = asyncio.run(_run())
        assert waiting == {}
        created = agent_db.create_task.await_args.kwargs
        assert (created["task_id"], created["project_id"], created["status"]) == ("task-r1", "proj-1", "init")
        ((task, envelope),) = assigned
        assert (task.task_id, task.project_id, task.requirement) == ("task-r1", "proj-1", "build it")
        assert (envelope.payload["project_id"], envelope.metadata["origin_channel_id"]) == ("proj-1", "channel-web")

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

