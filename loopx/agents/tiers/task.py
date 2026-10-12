"""Task entity and TaskRouter: per-task Planner lifecycle management.

The TaskRouter receives TASK_REQUESTs from the Manager, creates a dedicated
PlannerAgent for each task, and manages the Planner lifecycle. When a task
completes or fails, the TaskRouter tears down the corresponding Planner and
forwards results to the Manager.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..broadcast import Broadcaster
from ..bus import MessageBus
from ..message import (
    MANAGER_ADDRESS,
    TASK_ROUTER_ADDRESS,
    AgentAddress,
    MessageEnvelope,
    MessageType,
    planner_address,
)
from .planner import PlannerAgent

logger = logging.getLogger(__name__)

from ...db import activity as db_activity, tasks as db_tasks

MAX_CONCURRENT_TASKS = 3


@dataclass
class Task:
    task_id: str
    requirement: str
    goal_id: str
    project_root: str
    status: str = "pending"  # "pending" | "assigned" | "running" | "completed" | "failed" | "cancelled"
    planner_id: str | None = None
    correlation_id: str = ""
    origin_channel_id: str = ""
    created_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "requirement": self.requirement,
            "goal_id": self.goal_id,
            "status": self.status,
            "planner_id": self.planner_id,
            "correlation_id": self.correlation_id,
        }


class TaskRouter:
    """Routes tasks to per-task PlannerAgent instances.

    Implements the ManagedAgent protocol so the Supervisor can manage it.
    Dynamically creates/destroys PlannerAgents as tasks arrive and complete.
    """

    def __init__(
        self,
        *,
        runtime_root: Path,
        broadcaster: Broadcaster | None = None,
    ) -> None:
        self._runtime_root = runtime_root
        self._bus: MessageBus | None = None
        self._tasks: dict[str, Task] = {}
        self._planners: dict[str, PlannerAgent] = {}
        self._semaphore = asyncio.Semaphore(MAX_CONCURRENT_TASKS)
        self._loop_task: asyncio.Task[Any] | None = None
        self._running = False
        self._pending_queue: asyncio.Queue[MessageEnvelope] = asyncio.Queue()
        self._broadcaster = broadcaster

    @property
    def agent_id(self) -> str:
        return "task-router"

    @property
    def agent_address(self) -> AgentAddress:
        return TASK_ROUTER_ADDRESS

    async def start(self, bus: MessageBus) -> None:
        self._bus = bus
        self._running = True
        self._loop_task = asyncio.create_task(self._message_loop())
        logger.info("task router started (max_concurrent=%d)", MAX_CONCURRENT_TASKS)

    async def stop(self) -> None:
        self._running = False
        if self._loop_task:
            self._loop_task.cancel()
            try:
                await self._loop_task
            except asyncio.CancelledError:
                pass
        for planner in list(self._planners.values()):
            await planner.stop()
        self._planners.clear()

    def is_healthy(self) -> bool:
        return self._running

    def list_tasks(self) -> list[dict[str, Any]]:
        return [t.to_dict() for t in self._tasks.values()]

    async def _message_loop(self) -> None:
        assert self._bus is not None
        try:
            async for envelope in self._bus.subscribe("task-router"):
                if not self._running:
                    break
                try:
                    await self._handle(envelope)
                except Exception:
                    logger.exception("task router error handling %s", envelope.message_id)
        except asyncio.CancelledError:
            pass

    async def _handle(self, envelope: MessageEnvelope) -> None:
        match envelope.message_type:
            case MessageType.TASK_REQUEST:
                await self._handle_task_request(envelope)
            case MessageType.GOAL_STATUS:
                await self._handle_planner_status(envelope)
            case MessageType.TASK_ACCEPTED:
                await self._handle_task_accepted(envelope)
            case MessageType.TASK_CANCEL:
                await self._handle_cancel(envelope)

    async def _handle_task_request(self, envelope: MessageEnvelope) -> None:
        assert self._bus is not None
        payload = envelope.payload
        task_id = f"task-{uuid.uuid4().hex[:12]}"

        task = Task(
            task_id=task_id,
            requirement=payload.get("requirement", ""),
            goal_id=payload.get("goal_id", ""),
            project_root=payload.get("project_root", ""),
            correlation_id=envelope.correlation_id,
            origin_channel_id=envelope.metadata.get("origin_channel_id", ""),
        )
        self._tasks[task_id] = task

        try:
            await db_tasks.create_task(
                task_id=task_id,
                requirement=task.requirement,
                goal_id=task.goal_id,
                project_root=task.project_root,
                correlation_id=task.correlation_id,
                origin_channel_id=task.origin_channel_id,
            )
        except Exception:
            logger.exception("failed to persist task %s", task_id)

        if self._semaphore.locked():
            task.status = "pending"
            self._pending_queue.put_nowait(envelope)
            logger.info("task %s queued (at capacity)", task_id)
            return

        await self._assign_task(task, envelope)

    async def _assign_task(self, task: Task, envelope: MessageEnvelope) -> None:
        assert self._bus is not None
        await self._semaphore.acquire()

        task.status = "assigned"
        planner = PlannerAgent(task_id=task.task_id, runtime_root=self._runtime_root)
        task.planner_id = planner.agent_id
        self._planners[task.task_id] = planner

        await planner.start(self._bus)
        task.status = "running"

        try:
            await db_tasks.update_task_status(
                task.task_id, "running", planner_id=planner.agent_id,
            )
        except Exception:
            logger.exception("failed to persist assign for task %s", task.task_id)

        await self._bus.publish(MessageEnvelope.create(
            source=TASK_ROUTER_ADDRESS,
            target=planner.agent_address,
            message_type=MessageType.TASK_REQUEST,
            payload=envelope.payload,
            correlation_id=envelope.correlation_id,
            metadata=envelope.metadata,
        ))
        logger.info("task %s assigned to %s", task.task_id, planner.agent_id)

    async def _handle_task_accepted(self, envelope: MessageEnvelope) -> None:
        assert self._bus is not None
        await self._bus.publish(MessageEnvelope.create(
            source=TASK_ROUTER_ADDRESS,
            target=MANAGER_ADDRESS,
            message_type=MessageType.TASK_ACCEPTED,
            payload=envelope.payload,
            correlation_id=envelope.correlation_id,
            metadata=envelope.metadata,
        ))

    async def _handle_planner_status(self, envelope: MessageEnvelope) -> None:
        assert self._bus is not None
        task_id = envelope.payload.get("task_id", "")
        status = envelope.payload.get("status", "")
        task = self._tasks.get(task_id)
        if not task:
            return

        try:
            await db_tasks.update_task_status(task_id, status)
            await db_activity.insert_activity(
                task_id=task_id,
                worker="planner",
                kind="phase",
                summary=envelope.payload.get("summary", status),
            )
        except Exception:
            logger.exception("failed to persist status for task %s", task_id)

        if self._broadcaster:
            self._broadcaster.publish(task_id, {
                "kind": "phase",
                "summary": envelope.payload.get("summary", status),
                "worker": "planner",
            })

        await self._bus.publish(MessageEnvelope.create(
            source=TASK_ROUTER_ADDRESS,
            target=MANAGER_ADDRESS,
            message_type=MessageType.GOAL_STATUS,
            payload=envelope.payload,
            correlation_id=envelope.correlation_id,
            metadata=envelope.metadata,
        ))

        if status in ("completed", "failed"):
            await self._teardown_task(task_id)
            await self._try_dequeue()

    async def _handle_cancel(self, envelope: MessageEnvelope) -> None:
        assert self._bus is not None
        task_id = envelope.payload.get("task_id", "")
        task = self._tasks.get(task_id)
        if not task:
            return

        task.status = "cancelled"
        planner = self._planners.get(task_id)
        if planner:
            await self._bus.publish(MessageEnvelope.create(
                source=TASK_ROUTER_ADDRESS,
                target=planner.agent_address,
                message_type=MessageType.TASK_CANCEL,
                payload={"task_id": task_id},
                correlation_id=task.correlation_id,
            ))
            await self._teardown_task(task_id)
            await self._try_dequeue()

    async def _teardown_task(self, task_id: str) -> None:
        planner = self._planners.pop(task_id, None)
        if planner:
            await planner.stop()
            self._semaphore.release()
            logger.info("task %s planner torn down", task_id)

        task = self._tasks.get(task_id)
        if task and task.status not in ("completed", "failed", "cancelled"):
            task.status = "completed"

        if task:
            try:
                await db_tasks.update_task_status(task_id, task.status)
            except Exception:
                logger.exception("failed to persist teardown for task %s", task_id)

        if self._broadcaster:
            self._broadcaster.close_task(task_id)

    async def _try_dequeue(self) -> None:
        if self._pending_queue.empty():
            return
        envelope = self._pending_queue.get_nowait()
        task_id = None
        for tid, t in self._tasks.items():
            if t.status == "pending" and t.correlation_id == envelope.correlation_id:
                task_id = tid
                break
        if task_id:
            await self._assign_task(self._tasks[task_id], envelope)
