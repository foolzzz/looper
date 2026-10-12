"""Planner Agent: task decomposition, dispatch, and monitoring.

Each PlannerAgent instance handles a single task. It receives the initial
TASK_REQUEST, decomposes it into PlanSteps, dispatches Workers, collects
results, and applies verification strategy C (self-verify simple, spawn
Reviewer for complex). When the task completes or fails, the Planner
signals the TaskRouter to tear it down.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..bus import MessageBus
from ..message import (
    MANAGER_ADDRESS,
    TASK_ROUTER_ADDRESS,
    AgentAddress,
    MessageEnvelope,
    MessageType,
    planner_address,
    worker_address,
)

logger = logging.getLogger(__name__)

from ...db import steps as db_steps, activity as db_activity
from ...db.tasks import update_task_phase

MAX_STEPS = 8
MAX_CONCURRENT_WORKERS = 4


@dataclass
class PlanStep:
    step_id: str
    index: int
    instruction: str
    target_files: list[str] = field(default_factory=list)
    validation_commands: list[str] = field(default_factory=list)
    acceptance_criteria: list[str] = field(default_factory=list)
    depends_on: list[str] = field(default_factory=list)
    complexity: str = "simple"  # "simple" | "complex"
    status: str = "pending"  # "pending" | "dispatched" | "completed" | "failed" | "verified"
    result: dict[str, Any] | None = None
    worker_id: str | None = None


@dataclass
class TaskPlan:
    plan_id: str
    goal_id: str
    requirement: str
    project_root: str
    steps: list[PlanStep] = field(default_factory=list)
    status: str = "planning"  # "planning" | "executing" | "verifying" | "completed" | "failed"
    correlation_id: str = ""
    origin_channel_id: str = ""

    @property
    def is_complete(self) -> bool:
        return all(s.status == "verified" for s in self.steps)

    @property
    def has_failed(self) -> bool:
        return any(s.status == "failed" for s in self.steps)

    def ready_steps(self) -> list[PlanStep]:
        """Steps whose dependencies are all verified."""
        done = {s.step_id for s in self.steps if s.status == "verified"}
        return [
            s for s in self.steps
            if s.status == "pending" and all(d in done for d in s.depends_on)
        ]

    def progress_summary(self) -> str:
        total = len(self.steps)
        done = sum(1 for s in self.steps if s.status == "verified")
        running = sum(1 for s in self.steps if s.status in ("dispatched", "completed"))
        failed = sum(1 for s in self.steps if s.status == "failed")
        parts = [f"{done}/{total} 完成"]
        if running:
            parts.append(f"{running} 执行中")
        if failed:
            parts.append(f"{failed} 失败")
        return ", ".join(parts)

    def to_dict(self) -> dict[str, Any]:
        return {
            "plan_id": self.plan_id,
            "goal_id": self.goal_id,
            "requirement": self.requirement,
            "status": self.status,
            "steps": [
                {
                    "step_id": s.step_id,
                    "index": s.index,
                    "instruction": s.instruction,
                    "status": s.status,
                    "complexity": s.complexity,
                    "target_files": s.target_files,
                }
                for s in self.steps
            ],
        }


class PlannerAgent:
    """Per-task Planner: one instance per task, one plan per instance."""

    def __init__(self, *, task_id: str, runtime_root: Path) -> None:
        self._task_id = task_id
        self._runtime_root = runtime_root
        self._bus: MessageBus | None = None
        self._plan: TaskPlan | None = None
        self._loop_task: asyncio.Task[Any] | None = None
        self._running = False
        self._plans_dir = runtime_root / ".local" / "planner" / f"planner-{task_id}"
        self._plans_dir.mkdir(parents=True, exist_ok=True)

    @property
    def agent_id(self) -> str:
        return f"planner-{self._task_id}"

    @property
    def agent_address(self) -> AgentAddress:
        return planner_address(self._task_id)

    async def start(self, bus: MessageBus) -> None:
        self._bus = bus
        self._running = True
        self._load_plan()
        self._loop_task = asyncio.create_task(self._message_loop())
        logger.info("planner %s started", self.agent_id)

    async def stop(self) -> None:
        self._running = False
        if self._loop_task:
            self._loop_task.cancel()
            try:
                await self._loop_task
            except asyncio.CancelledError:
                pass
        if self._plan:
            self._save_plan(self._plan)

    def is_healthy(self) -> bool:
        return self._running

    async def _message_loop(self) -> None:
        assert self._bus is not None
        try:
            async for envelope in self._bus.subscribe(self.agent_id):
                if not self._running:
                    break
                try:
                    await self._handle(envelope)
                except Exception:
                    logger.exception("planner %s error handling %s", self.agent_id, envelope.message_id)
        except asyncio.CancelledError:
            pass

    async def _handle(self, envelope: MessageEnvelope) -> None:
        match envelope.message_type:
            case MessageType.TASK_REQUEST:
                await self._plan_and_dispatch(envelope)
            case MessageType.WORK_RESULT:
                await self._handle_work_result(envelope)
            case MessageType.VERIFY_RESULT:
                await self._handle_verify_result(envelope)
            case MessageType.TASK_CANCEL:
                await self._handle_cancel(envelope)
            case MessageType.SYSTEM_EVENT:
                await self._handle_system_event(envelope)

    async def _plan_and_dispatch(self, envelope: MessageEnvelope) -> None:
        assert self._bus is not None
        payload = envelope.payload
        plan_id = f"plan-{uuid.uuid4().hex[:12]}"

        await update_task_phase(self._task_id, "planning")

        plan = TaskPlan(
            plan_id=plan_id,
            goal_id=payload.get("goal_id", ""),
            requirement=payload.get("requirement", ""),
            project_root=payload.get("project_root", ""),
            correlation_id=envelope.correlation_id,
            origin_channel_id=envelope.metadata.get("origin_channel_id", ""),
        )

        steps = self._decompose_task(payload)
        plan.steps = steps
        plan.status = "executing"
        await update_task_phase(self._task_id, "executing")
        self._plan = plan
        self._save_plan(plan)

        try:
            await db_steps.upsert_steps(self._task_id, [
                {
                    "id": s.step_id,
                    "index": s.index,
                    "instruction": s.instruction,
                    "target_files": s.target_files,
                    "status": s.status,
                }
                for s in steps
            ])
        except Exception:
            logger.exception("failed to persist steps for task %s", self._task_id)

        # Notify TaskRouter + Manager: task accepted
        await self._bus.publish(MessageEnvelope.create(
            source=self.agent_address,
            target=TASK_ROUTER_ADDRESS,
            message_type=MessageType.TASK_ACCEPTED,
            payload={
                "task_id": self._task_id,
                "plan_id": plan_id,
                "step_count": len(steps),
                "steps": [{"step_id": s.step_id, "instruction": s.instruction} for s in steps],
            },
            correlation_id=envelope.correlation_id,
            metadata=envelope.metadata,
        ))

        await self._report_progress(plan)

        for step in plan.ready_steps()[:MAX_CONCURRENT_WORKERS]:
            await self._dispatch_step(plan, step)

    def _decompose_task(self, payload: dict[str, Any]) -> list[PlanStep]:
        requirement = payload.get("requirement", "")
        step = PlanStep(
            step_id=f"step-{uuid.uuid4().hex[:8]}",
            index=0,
            instruction=requirement,
            complexity="simple",
        )
        return [step]

    async def _dispatch_step(self, plan: TaskPlan, step: PlanStep) -> None:
        assert self._bus is not None
        worker_id = f"worker-{uuid.uuid4().hex[:12]}"
        step.status = "dispatched"
        step.worker_id = worker_id

        try:
            await db_steps.update_step_status(
                step.step_id, "dispatched", worker_id=worker_id,
            )
            await db_activity.insert_activity(
                task_id=self._task_id,
                worker=worker_id,
                kind="phase",
                summary=f"Worker dispatched: {step.instruction[:80]}",
            )
        except Exception:
            logger.exception("failed to persist dispatch for step %s", step.step_id)

        await self._bus.publish(MessageEnvelope.create(
            source=self.agent_address,
            target=worker_address(worker_id),
            message_type=MessageType.WORK_ITEM,
            payload={
                "task_id": step.step_id,
                "parent_task_id": self._task_id,
                "plan_id": plan.plan_id,
                "step_index": step.index,
                "instruction": step.instruction,
                "target_files": step.target_files,
                "validation_commands": step.validation_commands,
                "acceptance_criteria": step.acceptance_criteria,
                "context": {"project_root": plan.project_root},
            },
            correlation_id=plan.correlation_id,
            metadata={"origin_channel_id": plan.origin_channel_id},
        ))
        self._save_plan(plan)

    async def _handle_work_result(self, envelope: MessageEnvelope) -> None:
        assert self._bus is not None
        result = envelope.payload
        plan = self._plan
        if not plan:
            return

        task_id = result.get("task_id", "")
        step = next((s for s in plan.steps if s.step_id == task_id), None)
        if not step:
            return

        step.result = result

        if result.get("status") == "completed":
            if step.complexity == "complex":
                step.status = "completed"
                await update_task_phase(self._task_id, "verifying")
                await self._spawn_reviewer(plan, step)
            else:
                step.status = "verified"
        elif result.get("status") == "failed":
            step.status = "failed"

        self._save_plan(plan)

        try:
            await db_steps.update_step_status(
                step.step_id, step.status, result=result,
            )
            await db_activity.insert_activity(
                task_id=self._task_id,
                worker=step.worker_id or "worker",
                kind="text" if step.status == "verified" else "error",
                summary=result.get("summary", step.status)[:200],
            )
        except Exception:
            logger.exception("failed to persist work result for step %s", step.step_id)

        await self._report_progress(plan)

        if plan.is_complete:
            await self._complete_plan(plan)
        elif plan.has_failed:
            await self._fail_plan(plan)
        else:
            for next_step in plan.ready_steps()[:MAX_CONCURRENT_WORKERS]:
                await self._dispatch_step(plan, next_step)

    async def _spawn_reviewer(self, plan: TaskPlan, step: PlanStep) -> None:
        assert self._bus is not None
        reviewer_id = f"reviewer-{uuid.uuid4().hex[:12]}"

        await self._bus.publish(MessageEnvelope.create(
            source=self.agent_address,
            target=worker_address(reviewer_id),
            message_type=MessageType.VERIFY_REQUEST,
            payload={
                "task_id": step.step_id,
                "plan_id": plan.plan_id,
                "work_result": step.result or {},
                "acceptance_criteria": step.acceptance_criteria,
            },
            correlation_id=plan.correlation_id,
            metadata={"origin_channel_id": plan.origin_channel_id},
        ))

    async def _handle_verify_result(self, envelope: MessageEnvelope) -> None:
        result = envelope.payload
        plan = self._plan
        if not plan:
            return

        task_id = result.get("task_id", "")
        step = next((s for s in plan.steps if s.step_id == task_id), None)
        if not step:
            return

        if result.get("passed", False):
            step.status = "verified"
        else:
            step.status = "failed"
            step.result = step.result or {}
            step.result["verify_findings"] = result.get("findings", [])

        self._save_plan(plan)

        try:
            await db_steps.update_step_status(
                step.step_id, step.status, result=step.result,
            )
            await db_activity.insert_activity(
                task_id=self._task_id,
                worker="reviewer",
                kind="text" if step.status == "verified" else "error",
                summary=f"Verify {'passed' if result.get('passed') else 'failed'}: {result.get('summary', '')[:150]}",
            )
        except Exception:
            logger.exception("failed to persist verify result for step %s", step.step_id)

        await self._report_progress(plan)

        if plan.is_complete:
            await self._complete_plan(plan)
        elif plan.has_failed:
            await self._fail_plan(plan)

    async def _handle_cancel(self, envelope: MessageEnvelope) -> None:
        logger.info("planner %s received cancel", self.agent_id)
        if self._plan:
            self._plan.status = "failed"
            self._save_plan(self._plan)
        self._running = False

    async def _handle_system_event(self, envelope: MessageEnvelope) -> None:
        event_type = envelope.payload.get("type", "")
        if event_type == "pause":
            scope = envelope.payload.get("scope", "")
            logger.info("planner %s pausing work related to %s", self.agent_id, scope)

    async def _complete_plan(self, plan: TaskPlan) -> None:
        assert self._bus is not None
        plan.status = "completed"
        self._save_plan(plan)
        await update_task_phase(self._task_id, "completed")

        summary_parts = [f"任务完成: {plan.requirement[:80]}"]
        for step in plan.steps:
            changed = step.result.get("changed_files", {}) if step.result else {}
            if changed:
                summary_parts.append(f"  - {step.instruction[:50]}: 修改了 {len(changed)} 个文件")
            else:
                summary_parts.append(f"  - {step.instruction[:50]}: 完成")

        await self._bus.publish(MessageEnvelope.create(
            source=self.agent_address,
            target=TASK_ROUTER_ADDRESS,
            message_type=MessageType.GOAL_STATUS,
            payload={
                "task_id": self._task_id,
                "goal_id": plan.goal_id,
                "plan_id": plan.plan_id,
                "status": "completed",
                "progress": plan.progress_summary(),
                "summary": "\n".join(summary_parts),
            },
            correlation_id=plan.correlation_id,
            metadata={"origin_channel_id": plan.origin_channel_id},
        ))

    async def _fail_plan(self, plan: TaskPlan) -> None:
        assert self._bus is not None
        plan.status = "failed"
        self._save_plan(plan)
        await update_task_phase(self._task_id, "failed")

        failed_steps = [s for s in plan.steps if s.status == "failed"]
        errors = [
            f"  - {s.instruction[:50]}: {(s.result or {}).get('error', '未知错误')}"
            for s in failed_steps
        ]

        await self._bus.publish(MessageEnvelope.create(
            source=self.agent_address,
            target=TASK_ROUTER_ADDRESS,
            message_type=MessageType.GOAL_STATUS,
            payload={
                "task_id": self._task_id,
                "goal_id": plan.goal_id,
                "plan_id": plan.plan_id,
                "status": "failed",
                "progress": plan.progress_summary(),
                "summary": f"任务部分失败:\n" + "\n".join(errors),
            },
            correlation_id=plan.correlation_id,
            metadata={"origin_channel_id": plan.origin_channel_id},
        ))

    async def _report_progress(self, plan: TaskPlan) -> None:
        assert self._bus is not None
        await self._bus.publish(MessageEnvelope.create(
            source=self.agent_address,
            target=TASK_ROUTER_ADDRESS,
            message_type=MessageType.GOAL_STATUS,
            payload={
                "task_id": self._task_id,
                "goal_id": plan.goal_id,
                "plan_id": plan.plan_id,
                "status": plan.status,
                "progress": plan.progress_summary(),
                "summary": f"进度更新: {plan.progress_summary()}",
            },
            correlation_id=plan.correlation_id,
            metadata={"origin_channel_id": plan.origin_channel_id},
        ))

    # --- Persistence ---

    def _save_plan(self, plan: TaskPlan) -> None:
        path = self._plans_dir / f"{plan.plan_id}.json"
        data = plan.to_dict()
        data["correlation_id"] = plan.correlation_id
        data["origin_channel_id"] = plan.origin_channel_id
        data["project_root"] = plan.project_root
        path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")

    def _load_plan(self) -> None:
        if not self._plans_dir.exists():
            return
        for path in self._plans_dir.glob("*.json"):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                if data.get("status") in ("planning", "executing", "verifying"):
                    self._plan = TaskPlan(
                        plan_id=data["plan_id"],
                        goal_id=data.get("goal_id", ""),
                        requirement=data.get("requirement", ""),
                        project_root=data.get("project_root", ""),
                        status=data.get("status", "executing"),
                        correlation_id=data.get("correlation_id", ""),
                        origin_channel_id=data.get("origin_channel_id", ""),
                    )
                    for sd in data.get("steps", []):
                        self._plan.steps.append(PlanStep(
                            step_id=sd["step_id"],
                            index=sd.get("index", 0),
                            instruction=sd.get("instruction", ""),
                            status=sd.get("status", "pending"),
                            complexity=sd.get("complexity", "simple"),
                            target_files=sd.get("target_files", []),
                        ))
                    break
            except (json.JSONDecodeError, KeyError):
                logger.warning("skipping corrupt plan file %s", path)
