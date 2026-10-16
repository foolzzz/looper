"""Task entity and TaskRouter: per-task Planner lifecycle management.

The TaskRouter receives TASK_REQUESTs from the Manager and creates each task
ready (``tasks.READY``), without a Planner. A user starts it, from the
dashboard or through the Manager (TASK_START); the TaskRouter then creates a
dedicated PlannerAgent for it and manages the Planner lifecycle. When a task
completes or fails, the TaskRouter tears down the corresponding Planner and
forwards results to the Manager. Every task belongs to a project
(``project_id``), which can change while the task runs. At most the Planner's
concurrency limit of a project's tasks run at once (``agent_settings.load_concurrency``,
read each time one may start); the others wait, oldest first, without
holding up another project's. At server start it resumes the tasks the
previous server was running, from their plans in the database
(``resume_tasks``). Each task that ends (completed, failed, cancelled or
stopped) is reported to ``on_task_end``: the scheduler records a scheduled
run's outcome there.
"""

from __future__ import annotations

import asyncio
import logging
import os
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .. import step_workspaces
from ..agent_settings import AgentType, load_concurrency
from ..broadcast import Broadcaster
from ..bus import MessageBus
from ..message import (
    MANAGER_ADDRESS,
    TASK_ROUTER_ADDRESS,
    AgentAddress,
    MessageEnvelope,
    MessageType,
    RestartMode,
    planner_address,
)
from ..task_notice import Notice, NotifyTarget, ends_task, notice_text, notify_channel
from .planner import PlannerAgent, closing_note

logger = logging.getLogger(__name__)

from ...db import activity as db_activity, plans as db_plans, tasks as db_tasks

# Told each task's id and final status when it ends.
TaskEndListener = Callable[[str, str], Awaitable[None]]


def resolve_work_dir(raw: str | None) -> str:
    """The absolute directory a task runs in; there is no default.

    ``~`` is expanded and the path normalized. Raises ``ValueError`` when the
    directory is missing or relative, so callers can ask for a real one.
    """
    text = (raw or "").strip()
    if not text:
        raise ValueError("a work directory is required")
    path = os.path.expanduser(text)
    if not os.path.isabs(path):
        raise ValueError(f"work directory must be an absolute path, got {text!r}")
    return os.path.normpath(path)


@dataclass
class Task:
    task_id: str
    requirement: str
    goal_id: str
    work_dir: str
    project_id: str
    status: str = "pending"  # "pending" | "assigned" | "running" | "completed" | "failed" | "cancelled"
    planner_id: str | None = None
    correlation_id: str = ""
    origin_channel_id: str = ""
    notify: NotifyTarget | None = None  # who is told when it ends (``task_notice``)
    title: str = ""  # the Manager's few words naming it (``db.tasks.task_name``)
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
        claude_bin: str = "claude",
        on_task_end: TaskEndListener | None = None,
        dashboard_url: str = "",
    ) -> None:
        self._runtime_root = runtime_root
        self._on_task_end = on_task_end
        # Where a task's end notice links to (``links``; empty: relative).
        self._dashboard_url = dashboard_url
        self._claude_bin = claude_bin  # each task's Planner runs it
        self._bus: MessageBus | None = None
        self._tasks: dict[str, Task] = {}
        self._planners: dict[str, PlannerAgent] = {}
        self._loop_task: asyncio.Task[Any] | None = None
        self._running = False
        # The tasks waiting for room in their project, oldest first, each with the message that starts it.
        self._pending: list[tuple[str, MessageEnvelope]] = []
        # Held to change the waiting tasks, and for a whole pass over them (``recheck``), so two passes cannot
        # both take a project's last place.
        self._admitting = asyncio.Lock()
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
        logger.info("task router started")

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

    def running_planners(self) -> list[dict[str, Any]]:
        """Tasks with a live Planner: the Planners a user can chat with."""
        planners = []
        for task_id, planner in self._planners.items():
            task = self._tasks.get(task_id)
            planners.append({
                "task_id": task_id,
                "planner_id": planner.agent_id,
                "requirement": task.requirement if task else "",
                "name": db_tasks.task_name(title=task.title, goal_id=task.goal_id, requirement=task.requirement,
                                           task_id=task_id) if task else task_id,
                "status": task.status if task else "running",
                "project_id": task.project_id if task else None,
            })
        return planners

    def has_planner(self, task_id: str) -> bool:
        return task_id in self._planners

    def set_task_project(self, task_id: str, project_id: str) -> None:
        """Record a moved task's new project on its live entry, if the router holds one."""
        task = self._tasks.get(task_id)
        if task is not None:
            task.project_id = project_id

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
            case MessageType.TASK_START:
                await self._handle_start(envelope)
            case MessageType.GOAL_STATUS:
                await self._handle_planner_status(envelope)
            case MessageType.TASK_ACCEPTED:
                await self._handle_task_accepted(envelope)
            case MessageType.TASK_CANCEL:
                await self._handle_cancel(envelope)
            case MessageType.TASK_STOP:
                await self._handle_stop(envelope)
            case MessageType.TASK_RESTART:
                await self._handle_restart(envelope)

    async def resume_tasks(self, task_ids: list[str]) -> None:
        """Give each task the previous server was running a new Planner that carries on with its saved plan.

        Like a new task, one waits while its project runs as many tasks as its limit.
        """
        for task_id in task_ids:
            try:
                row = await db_tasks.get_task(task_id)
            except Exception:
                logger.exception("cannot read task %s to resume it", task_id)
                continue
            if row is None:
                continue
            task = Task(
                task_id=task_id,
                requirement=row["requirement"],
                goal_id=row.get("goal_id") or "",
                work_dir=row.get("work_dir") or "",
                project_id=row["project_id"],
                correlation_id=row.get("correlation_id") or "",
                origin_channel_id=row.get("origin_channel_id") or "",
                notify=NotifyTarget.from_columns(row),
                title=row.get("title") or "",
            )
            self._tasks[task_id] = task
            envelope = MessageEnvelope.create(
                source=TASK_ROUTER_ADDRESS,
                target=TASK_ROUTER_ADDRESS,
                message_type=MessageType.TASK_RESUME,
                payload={"task_id": task_id},
                correlation_id=task.correlation_id,
                metadata={"origin_channel_id": task.origin_channel_id},
            )
            await self._start_or_queue(task, envelope)

    async def _handle_task_request(self, envelope: MessageEnvelope) -> None:
        """Create the requested task ready, without a Planner; it runs once a user starts it (``TASK_START``)."""
        payload = envelope.payload
        project_id = payload.get("project_id") or ""
        if not project_id:
            # Every task belongs to a project. A request without one predates
            # projects (replayed from the bus journal); creating it would fail.
            logger.error("dropping task request %s: it names no project", envelope.message_id)
            return
        # The Manager names the task, so it can show and start it, and a request replayed from the journal
        # creates it once.
        task_id = payload.get("task_id") or f"task-{uuid.uuid4().hex[:12]}"
        try:
            await db_tasks.create_task(
                task_id=task_id,
                requirement=payload.get("requirement", ""),
                goal_id=payload.get("goal_id", ""),
                work_dir=payload.get("work_dir", ""),
                project_id=project_id,
                status=db_tasks.READY,
                correlation_id=envelope.correlation_id,
                origin_channel_id=envelope.metadata.get("origin_channel_id") or "",
                chat_id=payload.get("chat_id") or None,
                original_user_text=payload.get("original_user_text") or None,
                title=payload.get("title"),
            )
        except Exception:
            logger.exception("failed to persist task %s", task_id)
            return
        logger.info("task %s created ready in project %s", task_id, project_id)

    async def _handle_start(self, envelope: MessageEnvelope) -> None:
        """Start a ready task, once: like a new task, it waits while its project runs as many tasks as its limit."""
        task_id = envelope.payload.get("task_id", "")
        try:
            row = await db_tasks.start_ready_task(task_id)
        except Exception:
            logger.exception("failed to start task %s", task_id)
            return
        if row is None:
            logger.warning("task %s is not ready (unknown or started already); not starting it", task_id)
            return
        task = Task(
            task_id=task_id,
            requirement=row["requirement"],
            goal_id=row.get("goal_id") or "",
            work_dir=row.get("work_dir") or "",
            project_id=row["project_id"],
            correlation_id=row.get("correlation_id") or "",
            origin_channel_id=row.get("origin_channel_id") or "",
            notify=NotifyTarget.from_columns(row),
            title=row.get("title") or "",
        )
        self._tasks[task_id] = task
        await self._start_or_queue(task, MessageEnvelope.create(
            source=TASK_ROUTER_ADDRESS,
            target=TASK_ROUTER_ADDRESS,
            message_type=MessageType.TASK_REQUEST,
            payload={"requirement": task.requirement, "goal_id": task.goal_id, "work_dir": task.work_dir,
                     "project_id": task.project_id},
            # A task asked for in a chat reports its plan and its outcome there.
            correlation_id=task.correlation_id,
            metadata={"origin_channel_id": task.origin_channel_id},
        ))

    async def _start_or_queue(self, task: Task, envelope: MessageEnvelope) -> None:
        """Queue ``task`` behind its project's waiting tasks, then start every one that has room."""
        async with self._admitting:
            task.status = "pending"
            self._pending = [(tid, env) for tid, env in self._pending if tid != task.task_id]
            self._pending.append((task.task_id, envelope))
        await self.recheck()
        if task.status == "pending":
            logger.info("task %s queued: project %s runs as many tasks as its limit", task.task_id, task.project_id)

    def _running_in(self, project_id: str) -> int:
        """The project's tasks with a Planner; a moved task counts in its new project."""
        return sum(1 for task_id in self._planners
                   if (task := self._tasks.get(task_id)) is not None and task.project_id == project_id)

    async def recheck(self) -> None:
        """Start the waiting tasks whose project has room under its limit, read now, oldest first in each project.

        Runs when a task arrives or ends, and when a limit changes: a raised limit starts waiting tasks at
        once, a lowered one stops nothing that runs.
        """
        async with self._admitting:
            limits: dict[str, int] = {}
            for task_id, envelope in list(self._pending):
                task = self._tasks.get(task_id)
                if task is None or task.status != "pending" or task_id in self._planners:
                    self._pending.remove((task_id, envelope))  # stopped or cancelled while it waited
                    continue
                project_id = task.project_id
                if project_id not in limits:
                    limits[project_id] = await load_concurrency(AgentType.PLANNER, project_id)
                if self._running_in(project_id) >= limits[project_id]:
                    continue
                self._pending.remove((task_id, envelope))
                await self._assign_task(task, envelope)

    async def _assign_task(self, task: Task, envelope: MessageEnvelope) -> None:
        assert self._bus is not None
        task.status = "assigned"
        planner = PlannerAgent(task_id=task.task_id, runtime_root=self._runtime_root, claude_bin=self._claude_bin)
        task.planner_id = planner.agent_id
        self._planners[task.task_id] = planner
        # What an earlier Planner of the task left unread is not for this one: the cancel of a stop that came
        # during its turn would end this Planner at once, and a late result answers no step it runs.
        stale = await self._bus.drain_queue(planner.agent_id)
        if stale:
            logger.info("task %s: dropped %d message(s) its earlier Planner left unread", task.task_id, len(stale))

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
            # A resumed task's Planner carries on with the plan it read at start; any other plans the task.
            message_type=(MessageType.TASK_RESUME if envelope.message_type is MessageType.TASK_RESUME
                          else MessageType.TASK_REQUEST),
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
        if not task or ends_task(task.status):
            return  # an ended task's notice is its last message (``task_notice``)

        summary = envelope.payload.get("summary") or None
        fail_kind = envelope.payload.get("fail_kind") if status == "failed" else None
        try:
            # The Planner's final summary is the task's result, or its error.
            await db_tasks.update_task_status(
                task_id, status,
                result_summary=summary if status == "completed" else None,
                error=summary if status == "failed" else None,
                fail_kind=db_tasks.FailKind(fail_kind) if fail_kind else None,
            )
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

        payload = envelope.payload
        if ends_task(status) and (notice := self._notice(task, status, summary)) is not None:
            payload = {**payload, "notice": notice.payload()}
        await self._bus.publish(MessageEnvelope.create(
            source=TASK_ROUTER_ADDRESS,
            target=MANAGER_ADDRESS,
            message_type=MessageType.GOAL_STATUS,
            payload=payload,
            correlation_id=envelope.correlation_id,
            metadata=envelope.metadata,
        ))

        if ends_task(status):
            task.status = status  # teardown persists this; it would default to "completed"
            await self._teardown_task(task_id)
            await self.recheck()

    def _notice(self, task: Task, status: str, summary: str | None) -> Notice | None:
        """The end notice of a Telegram or Lark task that recorded who to tell (``task_notice``); None for any
        other. The Manager sends it behind the task's progress."""
        channel = notify_channel(task.origin_channel_id)
        if channel is None or task.notify is None:
            return None
        name = db_tasks.task_name(title=task.title, goal_id=task.goal_id, requirement=task.requirement,
                                  task_id=task.task_id)
        text = notice_text(task.task_id, name, status, summary, channel=channel, dashboard_url=self._dashboard_url)
        return Notice(channel, task.notify, text)

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
            await self.recheck()

    async def _handle_stop(self, envelope: MessageEnvelope) -> None:
        assert self._bus is not None
        task_id = envelope.payload.get("task_id", "")
        task = self._tasks.get(task_id)
        if not task:
            try:
                await db_tasks.update_task_status(task_id, "stopped", result_summary=await self._stop_note(task_id))
            except Exception:
                logger.exception("failed to mark orphan task %s stopped", task_id)
            await self._task_ended(task_id, "stopped")
            return

        task.status = "stopped"
        planner = self._planners.get(task_id)
        if planner:
            await self._bus.publish(MessageEnvelope.create(
                source=TASK_ROUTER_ADDRESS,
                target=planner.agent_address,
                message_type=MessageType.TASK_CANCEL,
                payload={"task_id": task_id},
                correlation_id=task.correlation_id,
            ))

        try:
            await db_tasks.update_task_status(task_id, "stopped", result_summary=await self._stop_note(task_id))
        except Exception:
            logger.exception("failed to persist stop for task %s", task_id)

        if planner:
            plnr = self._planners.pop(task_id, None)
            if plnr:
                await plnr.stop()
        if self._broadcaster:
            self._broadcaster.close_task(task_id)

        logger.info("task %s stopped", task_id)
        await self._task_ended(task_id, "stopped")
        await self.recheck()

    @staticmethod
    async def _stop_note(task_id: str) -> str:
        """A stopped task's closing note, from its saved plan: what its steps delivered and what is not done."""
        why = "The task was stopped before it finished."
        try:
            saved = await db_plans.load(task_id)
        except Exception:
            logger.warning("cannot read the plan of stopped task %s", task_id, exc_info=True)
            saved = None
        steps = (saved or {}).get("steps") or []
        return closing_note(why, ((str(s.get("step_key") or s.get("id")), str(s.get("status")), s.get("result"))
                                  for s in steps))

    async def _handle_restart(self, envelope: MessageEnvelope) -> None:
        """Run a task again as its ``RestartMode`` says: continued from its saved plan (the default), planned anew,
        or continued at a raised spend cap."""
        assert self._bus is not None
        task_id = envelope.payload.get("task_id", "")
        try:
            mode = RestartMode(envelope.payload.get("mode") or RestartMode.CONTINUE)
        except ValueError:
            logger.warning("task %s: unknown restart mode %r, not restarting it", task_id, envelope.payload.get("mode"))
            return

        existing = self._tasks.get(task_id)
        if existing and existing.status in ("running", "assigned"):
            logger.warning("task %s already active, ignoring restart", task_id)
            return

        try:
            db_row = await db_tasks.get_task(task_id)
        except Exception:
            logger.exception("failed to read task %s for restart", task_id)
            return

        if not db_row:
            logger.warning("task %s not found in DB, ignoring restart", task_id)
            return

        restartable = ("stopped", "failed", "cancelled", "running", "executing", "assigned", "init")
        if db_row["status"] not in restartable:
            logger.warning("task %s status=%s not restartable", task_id, db_row["status"])
            return

        start_over = mode is RestartMode.START_OVER
        if start_over:
            # Planned anew: the steps go, and so do their worktrees and branches.
            try:
                await step_workspaces.remove_task(task_id)
            except Exception:
                logger.warning("could not remove the step worktrees of task %s", task_id, exc_info=True)
        try:
            await db_tasks.reset_task_for_restart(task_id, keep_steps=not start_over)
        except Exception:
            logger.exception("failed to reset task %s for restart", task_id)
            return

        task = Task(
            task_id=task_id,
            requirement=db_row["requirement"],
            goal_id=db_row.get("goal_id", ""),
            work_dir=db_row.get("work_dir", ""),
            project_id=db_row["project_id"],
            correlation_id=db_row.get("correlation_id", ""),
            origin_channel_id=db_row.get("origin_channel_id", ""),
            notify=NotifyTarget.from_columns(db_row),
            title=db_row.get("title") or "",
        )
        self._tasks[task_id] = task

        # The Planner plans the request, unless it carries on with the plan (``restart``) of a task that ran before
        # and ended as ``ended`` says.
        carry_on = not start_over and db_row["status"] != db_tasks.READY
        restart_envelope = MessageEnvelope.create(
            source=TASK_ROUTER_ADDRESS,
            target=TASK_ROUTER_ADDRESS,
            message_type=MessageType.TASK_REQUEST,
            payload={
                "requirement": task.requirement,
                "goal_id": task.goal_id,
                "work_dir": task.work_dir,
                "project_id": task.project_id,
                **({"restart": mode.value, "ended": db_row["status"]} if carry_on else {}),
            },
            correlation_id=task.correlation_id,
        )
        await self._start_or_queue(task, restart_envelope)

    async def _teardown_task(self, task_id: str) -> None:
        planner = self._planners.pop(task_id, None)
        if planner:
            await planner.stop()
            logger.info("task %s planner torn down", task_id)

        task = self._tasks.get(task_id)
        if task and task.status not in ("completed", "failed", "cancelled", "stopped"):
            task.status = "completed"

        if task:
            try:
                await db_tasks.update_task_status(task_id, task.status)
            except Exception:
                logger.exception("failed to persist teardown for task %s", task_id)
            await self._task_ended(task_id, task.status)

        if self._broadcaster:
            self._broadcaster.close_task(task_id)

    async def _task_ended(self, task_id: str, status: str) -> None:
        if self._on_task_end is None:
            return
        try:
            await self._on_task_end(task_id, status)
        except Exception:
            logger.exception("the task-end listener failed for task %s", task_id)


async def recheck_limits(task_router: TaskRouter, worker_pool: Any) -> None:
    """After a concurrency limit changed: start the tasks and runs that wait for room a raised limit now gives; a
    lowered limit stops nothing. A failure is logged: the limit stands, and what waits starts when a task or run
    ends."""
    try:
        await task_router.recheck()
        if worker_pool is not None:
            await worker_pool.recheck()
    except Exception:
        logger.exception("re-checking the concurrency limits failed")
