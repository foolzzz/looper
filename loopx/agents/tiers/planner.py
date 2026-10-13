"""Planner Agent: one per task, backed by a resident Claude Code process.

Each task has one PlannerAgent for its whole life, and the Planner keeps one
Claude Code process running for as long as the task runs: stream-json in and
out, in the task's directory, with read-only tools (it reads the project to
plan; Workers make the changes). Each event is one user turn on it:

- the TASK_REQUEST: Claude splits the requirement into steps, each with an
  instruction, acceptance criteria, dependencies and whether an independent
  Reviewer must pass it;
- each Worker or Reviewer result: Claude decides to accept the step, send it
  to review, retry it in a new Worker, replan the steps not started yet, or
  fail the task;
- each chat message from a user: Claude replies, and may pass the text to a
  running Worker or add steps.

Every reply ends with JSON that ``planner_protocol`` validates; code then
does the dispatching. The system prompt is rebuilt for every turn from the
``planner`` config and the task's project as it is now (its background
included); when it changes, Claude restarts and resumes its session. An invalid reply gets one repair turn. Planning that
still fails fails the task; a decision that still fails falls back to the
step's own verdict. When the task completes or fails, the Planner signals the
TaskRouter, which tears it down and ends its Claude.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, TypeVar

from ..bus import MessageBus
from ..claude_cli import ClaudeRunRecorder, ResidentClaude, isolation_args
from ..system_prompt import compose_system_prompt
from ..message import (
    TASK_ROUTER_ADDRESS,
    AgentAddress,
    MessageEnvelope,
    MessageType,
    planner_address,
    worker_address,
)
from .planner_protocol import (
    MAX_RETRIES,
    MAX_STEPS,
    REPLY_CONTRACT,
    ChatAction,
    Decision,
    DecisionContext,
    DecisionReply,
    PlanView,
    ProtocolError,
    ResultKind,
    StepSpec,
    SuperviseAction,
    TurnKind,
    parse_chat,
    parse_decision,
    parse_plan,
    parse_supervise,
)

logger = logging.getLogger(__name__)

from ...db import activity as db_activity, agent_configs as db_agent_configs
from ...db import projects as db_projects, steps as db_steps
from ...db.tasks import update_task_phase

MAX_CONCURRENT_WORKERS = 4
# How long to wait for the worker pool to confirm a forwarded user turn.
WORKER_INPUT_TIMEOUT = 5.0
# A Planner turn's time limit without a readable ``planner`` config row.
DEFAULT_TURN_TIMEOUT_SEC = 1200
# How often the Planner looks at the Workers running now when no result has
# arrived (a SUPERVISE turn), to steer or stop one that drifts or stalls.
# 0 turns the periodic check off. Any Planner turn re-arms the interval.
DEFAULT_SUPERVISE_INTERVAL_SEC = 300
# The only tools the Planner's Claude has: it reads the project, Workers change it.
PLANNER_TOOLS = "Read,Grep,Glob"

DEFAULT_SYSTEM_PROMPT = """\
You are the Planner of one LoopX task. You live as long as the task: you plan
its steps, decide what happens after each step's result, and answer the
user's messages about it.

- Workers do the work: each step runs in a fresh Claude Code session that sees
  only its step. You can read the project (Read, Grep, Glob) to plan well, but
  you cannot change files or run commands.
- Split the work into steps that each deliver one result that can be checked
  on its own (for example the code, then its tests, then its docs), and make
  a step depend on the steps whose results it needs. Keep a small piece of
  work whole when splitting it would only add hand-offs.
- Give each step an instruction a Worker can follow on its own and acceptance
  criteria that say how to tell it is done.
- Ask for an independent Reviewer when a step is risky or its result is hard
  to judge from the Worker's own report.
- Accept a step only when its result meets the acceptance criteria. Retry with
  specific feedback when a Worker can fix what is wrong; replan when the
  approach must change; fail when the task cannot be done.
- Keep replies short. Answer the user in their language.
"""

T = TypeVar("T")


@dataclass(frozen=True)
class PlannerSettings:
    """The ``planner`` agent config, read for every turn."""

    system_prompt: str
    timeout_seconds: int  # per turn; 0 for no limit
    model: str | None
    max_turns: int  # 0 for no limit
    supervise_interval_seconds: int  # periodic SUPERVISE turn; 0 to turn off


async def load_settings() -> PlannerSettings:
    """Read the ``planner`` agent config; without a readable row use the defaults."""
    try:
        config = await db_agent_configs.get_config("planner")
    except Exception:
        logger.warning("cannot read the planner agent config; using defaults", exc_info=True)
        config = None
    if config is None:
        return PlannerSettings(
            DEFAULT_SYSTEM_PROMPT, DEFAULT_TURN_TIMEOUT_SEC, None, 0, DEFAULT_SUPERVISE_INTERVAL_SEC,
        )
    interval = config.get("supervise_interval_seconds")
    return PlannerSettings(
        system_prompt=str(config.get("system_prompt") or "").strip() or DEFAULT_SYSTEM_PROMPT,
        timeout_seconds=int(config.get("timeout_seconds") or 0),
        model=str(config.get("model") or "").strip() or None,
        max_turns=int(config.get("max_turns") or 0),
        supervise_interval_seconds=(
            DEFAULT_SUPERVISE_INTERVAL_SEC if interval is None else max(0, int(interval))
        ),
    )


class PlannerTurnError(Exception):
    """A Planner turn produced no valid reply; the message says why."""


@dataclass
class PlanStep:
    step_id: str  # unique across tasks: the task_steps row id
    index: int
    instruction: str
    key: str = ""  # the Planner's label for the step, unique within the task
    target_files: list[str] = field(default_factory=list)
    validation_commands: list[str] = field(default_factory=list)
    acceptance_criteria: list[str] = field(default_factory=list)
    depends_on: list[str] = field(default_factory=list)  # step_ids
    review: bool = False  # a Reviewer must pass the step before it is accepted
    # "pending" | "dispatched" | "deciding" | "completed" (in review) | "verified" | "failed" | "skipped"
    status: str = "pending"
    result: dict[str, Any] | None = None
    worker_id: str | None = None
    reviewer_id: str | None = None
    retries: int = 0
    feedback: list[str] = field(default_factory=list)  # for retries
    # User text the Planner forwarded that reached this step's Worker too late.
    missed_messages: list[str] = field(default_factory=list)

    @property
    def label(self) -> str:
        return self.key or self.step_id

    @property
    def started(self) -> bool:
        return self.worker_id is not None


@dataclass
class TaskPlan:
    plan_id: str
    goal_id: str
    requirement: str
    work_dir: str
    steps: list[PlanStep] = field(default_factory=list)
    status: str = "planning"  # "planning" | "executing" | "verifying" | "completed" | "failed"
    correlation_id: str = ""
    origin_channel_id: str = ""
    project_id: str = ""
    # The task's project as last read; refreshed before every event's turns.
    project_name: str = ""
    project_description: str = ""
    project_background: str = ""
    session_id: str | None = None  # the Planner's Claude session, resumed after a restart
    final_summary: str = ""  # the last accepted step's summary
    fail_reason: str = ""

    @property
    def is_complete(self) -> bool:
        return all(s.status in ("verified", "skipped") for s in self.steps)

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

    def step(self, step_id: str) -> PlanStep | None:
        return next((s for s in self.steps if s.step_id == step_id), None)

    def labels(self) -> dict[str, str]:
        """step_id → label."""
        return {s.step_id: s.label for s in self.steps}

    def view(self) -> PlanView:
        labels = self.labels()

        def deps(step: PlanStep) -> tuple[str, ...]:
            return tuple(labels.get(d, d) for d in step.depends_on)

        live = [s for s in self.steps if s.status != "skipped"]
        dropped = [s for s in self.steps if s.status == "skipped"]
        return PlanView(
            started={s.label: deps(s) for s in live if s.started},
            pending={s.label: deps(s) for s in live if not s.started},
            retired=frozenset(s.label for s in dropped),
            spent=sum(1 for s in dropped if s.started),
            running=frozenset(s.label for s in live if s.status == "dispatched"),
        )

    def progress_summary(self) -> str:
        live = [s for s in self.steps if s.status != "skipped"]
        total = len(live)
        done = sum(1 for s in live if s.status == "verified")
        running = sum(1 for s in live if s.status in ("dispatched", "deciding", "completed"))
        failed = sum(1 for s in live if s.status == "failed")
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
            "project_id": self.project_id,
            "project_name": self.project_name,
            "project_description": self.project_description,
            "project_background": self.project_background,
            "session_id": self.session_id,
            "final_summary": self.final_summary,
            "fail_reason": self.fail_reason,
            "steps": [
                {
                    "step_id": s.step_id,
                    "index": s.index,
                    "key": s.key,
                    "instruction": s.instruction,
                    "status": s.status,
                    "acceptance_criteria": s.acceptance_criteria,
                    "review": s.review,
                    "target_files": s.target_files,
                    "depends_on": s.depends_on,
                    "worker_id": s.worker_id,
                    "reviewer_id": s.reviewer_id,
                    "retries": s.retries,
                    "feedback": s.feedback,
                }
                for s in self.steps
            ],
        }


def _clip(text: Any, limit: int) -> str:
    flat = " ".join(str(text or "").split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


def _prepare_work_dir(work_dir: str) -> str | None:
    """Create the task's directory if needed; the problem, if it cannot be used."""
    if not work_dir:
        return "the task has no work directory"
    try:
        Path(work_dir).mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return f"cannot create work directory {work_dir}: {exc}"
    return None


class PlannerAgent:
    """Per-task Planner: one instance and one resident Claude per task."""

    def __init__(self, *, task_id: str, runtime_root: Path, claude_bin: str = "claude") -> None:
        self._task_id = task_id
        self._runtime_root = runtime_root
        self._claude_bin = claude_bin
        self._bus: MessageBus | None = None
        self._plan: TaskPlan | None = None
        self._loop_task: asyncio.Task[Any] | None = None
        self._running = False
        self._plans_dir = runtime_root / ".local" / "planner" / f"planner-{task_id}"
        self._plans_dir.mkdir(parents=True, exist_ok=True)
        self._claude = ResidentClaude(label=self.agent_id)
        self._turn_lock = asyncio.Lock()
        self._supervise_task: asyncio.Task[Any] | None = None
        # When the last turn ended, so a periodic SUPERVISE is skipped right
        # after a result/chat/plan turn already looked at the plan.
        self._last_turn_at = 0.0
        # The project background the last turn's system prompt carried (None before the first turn).
        self._prompt_background: str | None = None

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
        self._supervise_task = asyncio.create_task(self._supervise_loop())
        logger.info("planner %s started", self.agent_id)

    async def stop(self) -> None:
        """Stop handling messages and end the Planner's Claude."""
        self._running = False
        supervise_task, self._supervise_task = self._supervise_task, None
        if supervise_task:
            supervise_task.cancel()
        loop_task, self._loop_task = self._loop_task, None
        cancelled = False
        try:
            if supervise_task:
                await asyncio.wait([supervise_task])
            # Stop the Workers this task still runs; otherwise they keep
            # changing files after the task is gone.
            await self._kill_running_workers()
            if loop_task:
                loop_task.cancel()
                # Unlike awaiting the task, this does not mistake its
                # cancellation for ours: a cancelled stop() still propagates.
                await asyncio.wait([loop_task])
        except asyncio.CancelledError:
            cancelled = True
            raise
        finally:
            # A turn the loop was in has already killed Claude; an idle one
            # ends when its stdin closes. Never leave it running.
            await self._claude.stop(kill=cancelled)
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
        if self._plan is not None and envelope.message_type is not MessageType.TASK_REQUEST:
            # Turns this event runs see the project as it is now, in their
            # message and in the system prompt (a new plan reads it itself).
            await self._refresh_project(self._plan)
        match envelope.message_type:
            case MessageType.TASK_REQUEST:
                await self._plan_and_dispatch(envelope)
            case MessageType.WORK_RESULT:
                await self._handle_work_result(envelope)
            case MessageType.VERIFY_RESULT:
                await self._handle_verify_result(envelope)
            case MessageType.TASK_CANCEL:
                await self._handle_cancel(envelope)
            case MessageType.USER_MESSAGE:
                await self._handle_user_message(envelope)
            case MessageType.SYSTEM_EVENT:
                await self._handle_system_event(envelope)

    # --- Planning ---

    async def _plan_and_dispatch(self, envelope: MessageEnvelope) -> None:
        assert self._bus is not None
        payload = envelope.payload
        await update_task_phase(self._task_id, "planning")
        await self._activity("phase", "Planner started: analyzing requirement")

        plan = TaskPlan(
            plan_id=f"plan-{uuid.uuid4().hex[:12]}",
            goal_id=payload.get("goal_id", ""),
            requirement=payload.get("requirement", ""),
            work_dir=payload.get("work_dir", ""),
            correlation_id=envelope.correlation_id,
            origin_channel_id=envelope.metadata.get("origin_channel_id", ""),
            project_id=payload.get("project_id", "") or "",
        )
        await self._refresh_project(plan)
        self._plan = plan
        # A new plan is a new conversation.
        async with self._turn_lock:
            await self._claude.stop()
        self._save_plan(plan)

        problem = _prepare_work_dir(plan.work_dir)
        if problem:
            await self._fail_plan(plan, reason=f"Planning failed: {problem}")
            return
        try:
            reply = await self._ask(TurnKind.PLAN, self._plan_message(plan), parse_plan)
        except PlannerTurnError as exc:
            await self._fail_plan(plan, reason=f"Planning failed: {exc}")
            return

        steps = self._add_steps(plan, reply.steps)
        plan.status = "executing"
        await update_task_phase(self._task_id, "executing")
        await self._activity("phase", f"Plan created: {len(steps)} step(s): {self._describe(plan, steps)}")
        self._save_plan(plan)
        await self._persist_steps(steps)

        # Notify TaskRouter + Manager: task accepted
        await self._bus.publish(MessageEnvelope.create(
            source=self.agent_address,
            target=TASK_ROUTER_ADDRESS,
            message_type=MessageType.TASK_ACCEPTED,
            payload={
                "task_id": self._task_id,
                "plan_id": plan.plan_id,
                "step_count": len(steps),
                "steps": [{"step_id": s.step_id, "instruction": s.instruction} for s in steps],
            },
            correlation_id=envelope.correlation_id,
            metadata=envelope.metadata,
        ))

        await self._report_progress(plan)
        await self._dispatch_ready(plan)

    def _add_steps(self, plan: TaskPlan, specs: tuple[StepSpec, ...]) -> list[PlanStep]:
        """Add or replace (same label, not started) steps; returns them in spec order.

        Labels and dependencies were validated against this plan.
        """
        by_label = {s.label: s for s in plan.steps if s.status != "skipped"}
        changed: list[PlanStep] = []
        for spec in specs:
            step = by_label.get(spec.id)
            if step is None:
                step = PlanStep(
                    step_id=f"step-{uuid.uuid4().hex[:8]}",
                    index=len(plan.steps),
                    instruction=spec.instruction,
                    key=spec.id,
                )
                plan.steps.append(step)
                by_label[spec.id] = step
            step.instruction = spec.instruction
            step.acceptance_criteria = list(spec.acceptance_criteria)
            step.review = spec.review
            changed.append(step)
        for spec, step in zip(specs, changed):
            step.depends_on = [by_label[label].step_id for label in spec.depends_on]
        return changed

    def _describe(self, plan: TaskPlan, steps: list[PlanStep]) -> str:
        labels = plan.labels()
        parts = []
        for step in steps:
            notes = []
            if step.depends_on:
                notes.append("after " + "+".join(labels.get(d, d) for d in step.depends_on))
            if step.review:
                notes.append("review")
            parts.append(step.label + (f" ({', '.join(notes)})" if notes else ""))
        return ", ".join(parts)

    async def _dispatch_ready(self, plan: TaskPlan) -> None:
        running = sum(1 for s in plan.steps if s.status == "dispatched")
        for step in plan.ready_steps()[: max(0, MAX_CONCURRENT_WORKERS - running)]:
            await self._dispatch_step(plan, step)

    async def _dispatch_step(self, plan: TaskPlan, step: PlanStep) -> None:
        assert self._bus is not None
        worker_id = f"worker-{uuid.uuid4().hex[:12]}"
        step.status = "dispatched"
        step.worker_id = worker_id
        instruction = step.instruction
        if step.feedback:
            instruction += (
                f"\n\nThis step is being retried (attempt {step.retries + 1}). "
                "Feedback on the previous attempt:\n" + "\n".join(f"- {f}" for f in step.feedback)
            )

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
                "instruction": instruction,
                "target_files": step.target_files,
                "validation_commands": step.validation_commands,
                "acceptance_criteria": step.acceptance_criteria,
                "context": {"work_dir": plan.work_dir},
            },
            correlation_id=plan.correlation_id,
            metadata={"origin_channel_id": plan.origin_channel_id},
        ))
        self._save_plan(plan)

    # --- Results and decisions ---

    async def _handle_work_result(self, envelope: MessageEnvelope) -> None:
        result = envelope.payload
        plan = self._plan
        if not plan or plan.status not in ("executing", "verifying"):
            return
        step = plan.step(result.get("task_id", ""))
        if step is None or step.status != "dispatched" or envelope.source.agent_id != step.worker_id:
            logger.info("planner %s ignores a result that is not for a running step", self.agent_id)
            return

        step.result = result
        succeeded = result.get("status") == "completed"
        await self._activity(
            "text" if succeeded else "error",
            str(result.get("summary") or result.get("error") or result.get("status") or "")[:200],
            worker=step.worker_id,
        )
        context = DecisionContext(
            step=step.label, result=ResultKind.WORK, succeeded=succeeded,
            review_required=step.review, retries=step.retries,
        )
        await self._decide(plan, step, context, self._work_report(step, result))

    async def _spawn_reviewer(self, plan: TaskPlan, step: PlanStep) -> None:
        assert self._bus is not None
        reviewer_id = f"reviewer-{uuid.uuid4().hex[:12]}"
        step.reviewer_id = reviewer_id
        await self._activity("phase", f"Reviewer dispatched for step {step.index + 1} ({step.label})", worker=reviewer_id)

        await self._bus.publish(MessageEnvelope.create(
            source=self.agent_address,
            target=worker_address(reviewer_id),
            message_type=MessageType.VERIFY_REQUEST,
            payload={
                "task_id": step.step_id,
                "parent_task_id": self._task_id,
                "plan_id": plan.plan_id,
                "instruction": step.instruction,
                "work_result": step.result or {},
                "acceptance_criteria": step.acceptance_criteria,
                "context": {"work_dir": plan.work_dir},
            },
            correlation_id=plan.correlation_id,
            metadata={"origin_channel_id": plan.origin_channel_id},
        ))

    async def _handle_verify_result(self, envelope: MessageEnvelope) -> None:
        result = envelope.payload
        plan = self._plan
        if not plan or plan.status not in ("executing", "verifying"):
            return
        step = plan.step(result.get("task_id", ""))
        if step is None or step.status != "completed" or envelope.source.agent_id != step.reviewer_id:
            logger.info("planner %s ignores a review that is not for a step in review", self.agent_id)
            return

        passed = result.get("passed") is True
        findings = result.get("findings") or []
        step.result = {**(step.result or {}), "verify_passed": passed, "verify_findings": findings,
                       "verify_summary": result.get("summary", "")}
        await self._activity(
            "text" if passed else "error",
            f"Verify {'passed' if passed else 'failed'}: {str(result.get('summary') or '')[:150]}",
            worker=step.reviewer_id,
        )
        context = DecisionContext(
            step=step.label, result=ResultKind.REVIEW, succeeded=passed,
            review_required=step.review, retries=step.retries,
        )
        await self._decide(plan, step, context, self._review_report(step, result))

    async def _decide(self, plan: TaskPlan, step: PlanStep, context: DecisionContext, report: list[str]) -> None:
        """Ask the Planner's Claude what to do with ``step``'s result, then do it.

        Without a valid decision the step's own verdict decides, and no
        Reviewer is spawned.
        """
        view = plan.view()
        step.status = "deciding"
        message = self._decision_message(plan, step, context, report)
        step.missed_messages.clear()
        try:
            reply = await self._ask(
                TurnKind.DECISION, message, lambda text: parse_decision(text, context, view),
            )
        except PlannerTurnError as exc:
            whose = "Reviewer's" if context.result is ResultKind.REVIEW else "Worker's own"
            step.status = "verified" if context.succeeded else "failed"
            await self._activity(
                "error",
                f"Warning: the Planner could not decide on step {step.index + 1} ({step.label}): {exc}. "
                f"The {whose} verdict decides: {step.status}.",
            )
            await self._step_settled(plan, step)
            return
        await self._apply_decision(plan, step, context, reply)

    async def _apply_decision(
        self, plan: TaskPlan, step: PlanStep, context: DecisionContext, reply: DecisionReply,
    ) -> None:
        decision = reply.decision
        note = f": {reply.reason}" if reply.reason else ""
        if decision is Decision.RETRY:
            note = f": {reply.feedback}"
        await self._activity("phase", f"Planner decision on step {step.index + 1} ({step.label}): {decision.value}{note}"[:500])

        if decision is Decision.ACCEPT:
            step.status = "verified"
            if reply.summary:
                plan.final_summary = reply.summary
        elif decision is Decision.REVIEW:
            step.review = True
            step.status = "completed"
            self._save_plan(plan)
            await self._persist_step(step)
            await update_task_phase(self._task_id, "verifying")
            await self._spawn_reviewer(plan, step)
            await self._report_progress(plan)
            return
        elif decision is Decision.RETRY:
            step.retries += 1
            step.feedback.append(reply.feedback)
            step.reviewer_id = None
            await self._dispatch_step(plan, step)
            await self._report_progress(plan)
            return
        elif decision is Decision.REPLAN:
            step.status = "verified" if context.acceptable else "skipped"
            await self._persist_step(step)
            await self._replan(plan, reply.steps)
        elif decision is Decision.FAIL:
            step.status = "failed"
            plan.fail_reason = reply.reason
        await self._step_settled(plan, step)

    async def _replan(self, plan: TaskPlan, specs: tuple[StepSpec, ...]) -> None:
        """Replace every step that has not started with ``specs``."""
        keep = {spec.id for spec in specs}
        dropped = [s for s in plan.steps if s.status == "pending" and s.label not in keep]
        for step in dropped:
            step.status = "skipped"
            await self._persist_step(step)
        steps = self._add_steps(plan, specs)
        await self._persist_steps(steps)
        summary = f"Plan changed: {self._describe(plan, steps) or 'no new steps'}"
        if dropped:
            summary += f"; dropped {', '.join(s.label for s in dropped)}"
        await self._activity("phase", summary)

    async def _step_settled(self, plan: TaskPlan, step: PlanStep) -> None:
        """Persist ``step``'s outcome, then finish the task or start the steps now ready."""
        self._save_plan(plan)
        await self._persist_step(step)
        await self._report_progress(plan)
        if plan.has_failed:
            await self._fail_plan(plan)
        elif plan.is_complete:
            await self._complete_plan(plan)
        else:
            await self._dispatch_ready(plan)

    # --- Chat ---

    async def _handle_cancel(self, envelope: MessageEnvelope) -> None:
        logger.info("planner %s received cancel", self.agent_id)
        if self._plan:
            self._plan.status = "failed"
            self._save_plan(self._plan)
        self._running = False

    async def _handle_user_message(self, envelope: MessageEnvelope) -> None:
        """Answer a chat message through the Planner's Claude, and do what it decides."""
        assert self._bus is not None
        plan = self._plan
        text = str(envelope.payload.get("text", "")).strip()
        if plan is None or plan.status == "planning":
            reply = "任务还在规划中，请稍后再发。"
        elif plan.status not in ("executing", "verifying"):
            reply = f"任务已结束（{plan.status}），不能再发消息。"
        else:
            reply = await self._chat(plan, text, envelope)

        await self._bus.publish(MessageEnvelope.create(
            source=self.agent_address,
            target=envelope.source,
            message_type=MessageType.AGENT_REPLY,
            payload={"text": reply},
            correlation_id=envelope.correlation_id,
            reply_to=envelope.message_id,
            metadata={"origin_channel_id": envelope.metadata.get("origin_channel_id")},
        ))

    async def _chat(self, plan: TaskPlan, text: str, envelope: MessageEnvelope) -> str:
        view = plan.view()
        try:
            reply = await self._ask(
                TurnKind.CHAT, self._chat_message(plan, text), lambda answer: parse_chat(answer, view),
            )
        except PlannerTurnError as exc:
            await self._activity("error", f"Planner could not answer a chat message: {exc}"[:500])
            return f"Planner 出错，没能处理这条消息：{exc}"

        if reply.action is ChatAction.FORWARD:
            step = next(s for s in plan.steps if s.label == reply.step and s.status == "dispatched")
            if await self._forward_to_worker(step, reply.text, envelope):
                return reply.reply
            # The Worker ended first; its result's decision turn gets the text instead.
            step.missed_messages.append(reply.text)
            await self._activity("phase", f"User message for step {step.index + 1} ({step.label}) arrived after "
                                          f"{step.worker_id} finished; kept for the Planner's decision")
            return f"{reply.reply}\n\n（{step.worker_id} 在消息送达前已经结束；处理它的结果时我会考虑这条消息。）"
        if reply.action is ChatAction.ADD_STEPS:
            steps = self._add_steps(plan, reply.steps)
            self._save_plan(plan)
            await self._persist_steps(steps)
            await self._activity("phase", f"Steps added from chat: {self._describe(plan, steps)}")
            await self._report_progress(plan)
            await self._dispatch_ready(plan)
            return reply.reply
        await self._activity("phase", f"Planner answered a chat message: {reply.reply}"[:300])
        return reply.reply

    async def _forward_to_worker(self, step: PlanStep, text: str, envelope: MessageEnvelope) -> bool:
        """Ask the worker pool to add a user message as the running worker's next turn."""
        message = (
            "A message from the user, passed on by the Planner while you work on this step. "
            "Take it into account, then continue:\n\n" + text
        )
        if not await self._send_worker_input(step, message, envelope.correlation_id):
            return False
        await self._activity(
            "phase", f"User message passed to {step.worker_id} (step {step.index + 1}): {text[:80]}",
        )
        return True

    async def _send_worker_input(self, step: PlanStep, text: str, correlation_id: str) -> bool:
        """Add ``text`` as the running worker's next user turn; True if it landed."""
        assert self._bus is not None and step.worker_id is not None
        try:
            ack = await self._bus.request(MessageEnvelope.create(
                source=self.agent_address,
                target=worker_address(step.worker_id),
                message_type=MessageType.WORKER_INPUT,
                payload={"text": text, "task_id": step.step_id},
                correlation_id=correlation_id,
            ), timeout=WORKER_INPUT_TIMEOUT)
        except asyncio.TimeoutError:
            return False
        return bool(ack.payload.get("delivered"))

    async def _handle_system_event(self, envelope: MessageEnvelope) -> None:
        event_type = envelope.payload.get("type", "")
        if event_type == "pause":
            scope = envelope.payload.get("scope", "")
            logger.info("planner %s pausing work related to %s", self.agent_id, scope)
        elif event_type == "supervise":
            await self._supervise()

    # --- Supervising the Workers running now ---

    async def _supervise_loop(self) -> None:
        """Wake on a timer and, when Workers are running, post a supervise tick
        onto the Planner's own mailbox so the check runs in the message loop,
        serialized with results and chat (never concurrently with them)."""
        assert self._bus is not None
        while self._running:
            interval = (await load_settings()).supervise_interval_seconds
            if interval <= 0:
                return  # the periodic check is off
            try:
                await asyncio.sleep(interval)
            except asyncio.CancelledError:
                return
            if not self._running:
                return
            plan = self._plan
            if plan is None or plan.status != "executing":
                continue
            if not any(s.status == "dispatched" for s in plan.steps):
                continue
            # A result/chat/plan turn within the last interval already looked
            # at the plan; skip this tick rather than spend a turn on nothing new.
            if self._last_turn_at and time.monotonic() - self._last_turn_at < interval:
                continue
            try:
                await self._bus.publish(MessageEnvelope.create(
                    source=self.agent_address,
                    target=self.agent_address,
                    message_type=MessageType.SYSTEM_EVENT,
                    payload={"type": "supervise"},
                ))
            except Exception:
                logger.debug("planner %s could not post a supervise tick", self.agent_id, exc_info=True)

    async def _supervise(self) -> None:
        """Look at the Workers running now and steer or stop the ones that need it."""
        plan = self._plan
        if plan is None or plan.status != "executing":
            return
        running = [s for s in plan.steps if s.status == "dispatched"]
        if not running:
            return
        reports = [await self._running_report(plan, s) for s in running]
        view = plan.view()
        try:
            reply = await self._ask(
                TurnKind.SUPERVISE,
                self._supervise_message(plan, running, reports),
                lambda text: parse_supervise(text, view.running),
            )
        except PlannerTurnError as exc:
            await self._activity("error", f"Planner could not supervise the running steps: {exc}"[:500])
            return
        for review in reply.reviews:
            step = next((s for s in plan.steps if s.label == review.step and s.status == "dispatched"), None)
            if step is None:
                continue  # the step finished between the turn and now
            if review.action is SuperviseAction.NUDGE:
                await self._nudge_worker(plan, step, review.text)
            elif review.action is SuperviseAction.KILL:
                await self._kill_and_decide(plan, step, review.reason)

    async def _nudge_worker(self, plan: TaskPlan, step: PlanStep, text: str) -> None:
        message = (
            "A course correction from the Planner while you work on this step. "
            "Take it into account, then continue:\n\n" + text
        )
        if await self._send_worker_input(step, message, plan.correlation_id):
            await self._activity(
                "phase", f"Planner steered step {step.index + 1} ({step.label}): {text[:80]}", worker=step.worker_id,
            )
        else:
            step.missed_messages.append(text)
            await self._activity(
                "phase", f"Planner steer for step {step.index + 1} ({step.label}) arrived after "
                         f"{step.worker_id} finished; kept for its decision",
            )

    async def _kill_and_decide(self, plan: TaskPlan, step: PlanStep, reason: str) -> None:
        """Stop a drifting step's Worker, then decide what happens to the step.

        A killed Worker publishes no result, so the stopped outcome is fed
        through the normal decision path (the Planner picks retry/replan/fail).
        """
        if not await self._kill_worker(step):
            return  # the Worker already finished; its real result will decide
        await self._activity("error", f"Planner stopped step {step.index + 1} ({step.label}): {reason}"[:300],
                             worker=step.worker_id)
        result = {"status": "stopped", "summary": f"The Planner stopped this Worker: {reason}"}
        step.result = result
        context = DecisionContext(
            step=step.label, result=ResultKind.WORK, succeeded=False,
            review_required=step.review, retries=step.retries,
        )
        await self._decide(plan, step, context, self._work_report(step, result))

    async def _kill_worker(self, step: PlanStep) -> bool:
        """Ask the worker pool to stop ``step``'s running Worker; True if it was running."""
        assert self._bus is not None and step.worker_id is not None
        try:
            ack = await self._bus.request(MessageEnvelope.create(
                source=self.agent_address,
                target=worker_address(step.worker_id),
                message_type=MessageType.KILL_WORK,
                payload={"task_id": step.step_id},
                correlation_id=self._plan.correlation_id if self._plan else "",
            ), timeout=WORKER_INPUT_TIMEOUT)
        except asyncio.TimeoutError:
            return False
        return bool(ack.payload.get("killed"))

    async def _kill_running_workers(self) -> None:
        """Stop every Worker this task still runs (on task stop/cancel)."""
        plan = self._plan
        if plan is None or self._bus is None:
            return
        for step in plan.steps:
            if step.status == "dispatched" and step.worker_id:
                try:
                    await self._kill_worker(step)
                except Exception:
                    logger.debug("planner %s could not stop worker %s", self.agent_id, step.worker_id, exc_info=True)

    # --- Turns on the Planner's Claude ---

    async def _ask(self, kind: TurnKind, message: str, parse: Callable[[str], T]) -> T:
        """Run one turn and parse its reply; an invalid reply gets one repair turn.

        Raises ``PlannerTurnError`` when Claude fails or times out, or when the
        repaired reply is still invalid.
        """
        reply = await self._turn(message)
        try:
            return parse(reply)
        except ProtocolError as first:
            await self._activity("error", f"Planner reply broke the {kind.value} contract ({first}); asking again"[:500])
            reply = await self._turn(
                f"[{kind.value}] Your last reply broke the reply contract: {first}\n"
                f"Answer the same request again. End with exactly one ```json block in the {kind.value} shape."
            )
            try:
                return parse(reply)
            except ProtocolError as second:
                raise PlannerTurnError(f"invalid reply after a repair turn: {second}") from None

    async def _turn(self, message: str) -> str:
        async with self._turn_lock:
            try:
                return await self._run_turn(message)
            finally:
                self._last_turn_at = time.monotonic()

    def _claude_command(self, system_prompt: str, settings: PlannerSettings, work_dir: str) -> list[str]:
        """The Planner's Claude, without ``--resume``: read-only tools, nothing else approved."""
        return [
            self._claude_bin,
            "--print",
            "--verbose",
            "--output-format", "stream-json",
            # Each event is one user turn on stdin; Claude idles between turns.
            "--input-format", "stream-json",
            # Partial deltas keep the turn's heartbeat current while Claude writes.
            "--include-partial-messages",
            # Only the read-only tools exist, they are the only approved ones,
            # and anything else is denied without asking.
            "--tools", PLANNER_TOOLS,
            "--allowedTools", PLANNER_TOOLS,
            "--permission-mode", "dontAsk",
            # None of the operator's personal settings, memory, hooks, plugins or MCP servers.
            *isolation_args(work_dir),
            "--append-system-prompt", system_prompt,
            *(["--model", settings.model] if settings.model else []),
            *(["--max-turns", str(settings.max_turns)] if settings.max_turns > 0 else []),
        ]

    async def _run_turn(self, message: str) -> str:
        """Send ``message`` as one turn and return Claude's final message; raises ``PlannerTurnError``."""
        plan = self._plan
        assert plan is not None
        settings = await load_settings()
        system_prompt = compose_system_prompt(
            settings.system_prompt,
            project_name=plan.project_name,
            background=plan.project_background,
            contract=REPLY_CONTRACT,
        )
        await self._note_background(plan.project_background)
        base = self._claude_command(system_prompt, settings, plan.work_dir)
        signature = tuple(base)
        if self._claude.stale(signature):
            # Claude exited, or the config or the project background changed:
            # the next process resumes the session.
            await self._claude.stop()
        start_error: str | None = None
        if self._claude.started:
            command = self._claude.command
        else:
            command = base + (["--resume", plan.session_id] if plan.session_id else [])
            try:
                await self._claude.start(command, cwd=plan.work_dir, signature=signature)
            except OSError as exc:
                start_error = f"cannot start Claude Code: {exc}"
            else:
                resumed = f", resuming session {plan.session_id}" if plan.session_id else ""
                await self._activity("phase", f"Planner Claude Code started (pid {self._claude.pid}{resumed})")

        recorder = ClaudeRunRecorder(
            agent_id=self.agent_id,
            task_id=self._task_id,
            command=command,
            cwd=plan.work_dir,
            input_text=message,
            session_id=plan.session_id,
            pid=self._claude.pid,
            system_prompt=system_prompt,
        )
        await recorder.start()
        if start_error:
            await recorder.finish(output_text=None, result_event=None, error=start_error)
            raise PlannerTurnError(start_error)

        async def on_event(event: dict[str, Any]) -> None:
            await recorder.record(event)
            session_id = event.get("session_id")
            if isinstance(session_id, str) and session_id and session_id != plan.session_id:
                plan.session_id = session_id
                self._save_plan(plan)

        limit = settings.timeout_seconds
        try:
            turn = await asyncio.wait_for(self._claude.run_turn(message, on_event), limit if limit > 0 else None)
        except TimeoutError:
            # The turn was cancelled, which killed Claude; the next turn resumes the session.
            error = f"the turn ran longer than the {limit} s limit (timeout_seconds)"
            await recorder.finish(output_text=None, result_event=None, error=error)
            raise PlannerTurnError(error) from None
        except Exception as exc:
            error = f"reading Claude failed: {type(exc).__name__}: {exc}"
            await recorder.finish(output_text=None, result_event=None, error=error)
            raise PlannerTurnError(error) from None
        except BaseException as exc:
            await recorder.finish(output_text=None, result_event=None,
                                  error=f"Planner stopped reading Claude: {type(exc).__name__}")
            raise

        result_event = turn.result_event
        final_text = str((result_event or {}).get("result") or "")
        error: str | None = None
        if result_event is None:
            error = f"Claude Code exited before finishing the turn (exit code {turn.exit_code})"
        elif result_event.get("is_error") or result_event.get("subtype") != "success":
            error = f"Claude Code turn did not succeed ({result_event.get('subtype') or 'error'})"
        elif not final_text.strip():
            error = "Claude Code returned an empty reply"
        await recorder.finish(
            output_text=final_text or None,
            result_event=result_event,
            error=error,
            cost_usd=self._claude.turn_cost(result_event),
        )
        if error:
            raise PlannerTurnError(error)
        return final_text

    # --- Turn messages ---

    def _context_lines(self, plan: TaskPlan) -> list[str]:
        project = plan.project_name or "(unknown)"
        if plan.project_description:
            project += f": {_clip(plan.project_description, 300)}"
        return [
            f"Task: {_clip(plan.requirement, 1500)}",
            f"Project: {project}",
            f"Working directory: {plan.work_dir}",
        ]

    def _plan_lines(self, plan: TaskPlan) -> list[str]:
        labels = plan.labels()
        lines = ["Plan:"]
        for step in plan.steps:
            deps = ", ".join(labels.get(d, d) for d in step.depends_on) or "none"
            lines.append(
                f"- {step.label} [{step.status}] {_clip(step.instruction, 200)} "
                f"(depends on: {deps}; review: {'yes' if step.review else 'no'}; retries: {step.retries})"
            )
        return lines

    def _plan_message(self, plan: TaskPlan) -> str:
        return "\n".join([
            "[PLAN] Plan this task.",
            *self._context_lines(plan),
            "",
            f"You may read the project first. Reply with the PLAN JSON: 1 to {MAX_STEPS} steps.",
        ])

    def _work_report(self, step: PlanStep, result: dict[str, Any]) -> list[str]:
        lines = [
            f"Result of step {step.label} from {step.worker_id}:",
            f"- status: {result.get('status') or 'unknown'}",
        ]
        if result.get("summary"):
            lines.append(f"- summary: {_clip(result['summary'], 1000)}")
        changed = result.get("changed_files")
        if isinstance(changed, dict) and changed:
            files = [f"{path} ({_clip(note, 80)})" if note else str(path) for path, note in list(changed.items())[:20]]
            more = f" (+{len(changed) - 20} more)" if len(changed) > 20 else ""
            lines.append(f"- changed files: {'; '.join(files)}{more}")
        deleted = result.get("deleted_files")
        if isinstance(deleted, list) and deleted:
            lines.append(f"- deleted files: {', '.join(str(p) for p in deleted[:20])}")
        if result.get("error") and result.get("error") != result.get("summary"):
            lines.append(f"- error: {_clip(result['error'], 500)}")
        if result.get("final_message"):
            lines.append(f"- the Worker's final message: {_clip(result['final_message'], 300)}")
        return lines

    def _review_report(self, step: PlanStep, result: dict[str, Any]) -> list[str]:
        lines = [
            f"Review of step {step.label} by {step.reviewer_id}:",
            f"- passed: {'yes' if result.get('passed') is True else 'no'}",
        ]
        if result.get("summary"):
            lines.append(f"- summary: {_clip(result['summary'], 1000)}")
        findings = result.get("findings")
        if isinstance(findings, list):
            lines.extend(f"- finding: {_clip(f, 300)}" for f in findings[:10])
        return lines

    async def _running_report(self, plan: TaskPlan, step: PlanStep) -> list[str]:
        """What step ``step``'s Worker has done lately, for a SUPERVISE turn."""
        lines = [f"Step {step.label} ({step.worker_id}): {_clip(step.instruction, 200)}"]
        try:
            rows = await db_activity.list_activity(self._task_id, since_seq=0, limit=400)
        except Exception:
            lines.append("  - (recent activity is unavailable)")
            return lines
        recent = [r for r in rows if r.get("worker") == step.worker_id][-6:]
        if not recent:
            lines.append("  - (no activity yet)")
        for row in recent:
            lines.append(f"  - [{row.get('kind')}] {_clip(row.get('summary'), 160)}")
        return lines

    def _supervise_message(self, plan: TaskPlan, running: list[PlanStep], reports: list[list[str]]) -> str:
        lines = [
            "[SUPERVISE] A periodic check on the Workers running now — no result has arrived.",
            *self._context_lines(plan),
            "",
            *self._plan_lines(plan),
            "",
            "Workers running now and what they have done lately:",
        ]
        for report in reports:
            lines += report
        lines += [
            "",
            "Review only the steps that need it (a drifting or stalled Worker): nudge one that "
            "is on a workable path, kill one whose direction is wrong. Leave the rest alone.",
            "Reply with the SUPERVISE JSON.",
        ]
        return "\n".join(lines)

    def _decision_message(
        self, plan: TaskPlan, step: PlanStep, context: DecisionContext, report: list[str],
    ) -> str:
        who = "Reviewer" if context.result is ResultKind.REVIEW else "Worker"
        allowed = []
        for decision in context.allowed():
            if decision is Decision.RETRY:
                allowed.append(f"retry ({MAX_RETRIES - context.retries} left)")
            else:
                allowed.append(decision.value)
        lines = [
            f"[DECISION] Step {step.label} (step {step.index + 1}) has its {who}'s result.",
            *self._context_lines(plan),
            "",
            *self._plan_lines(plan),
            "",
            *report,
        ]
        if step.missed_messages:
            lines += ["", "The user sent this to the step's Worker through you, but it arrived after the Worker finished:"]
            lines += [f"- {_clip(m, 500)}" for m in step.missed_messages]
        if context.result is ResultKind.WORK and context.succeeded and context.review_required:
            lines += ["", "This step needs a passing review before it can be accepted."]
        lines += ["", f"Allowed decisions now: {', '.join(allowed)}.", "Reply with the DECISION JSON."]
        return "\n".join(lines)

    def _chat_message(self, plan: TaskPlan, text: str) -> str:
        running = [s for s in plan.steps if s.status == "dispatched"]
        running_text = ", ".join(f"{s.label} ({s.worker_id})" for s in running) or "none"
        actions = ["none", "add_steps"] + (["forward (to a running step)"] if running else [])
        return "\n".join([
            "[CHAT] A message from the user.",
            *self._context_lines(plan),
            "",
            *self._plan_lines(plan),
            f"Running now: {running_text}",
            "",
            "User message:",
            text,
            "",
            f"Allowed actions: {', '.join(actions)}.",
            "Reply with the CHAT JSON.",
        ])

    # --- Endings ---

    async def _complete_plan(self, plan: TaskPlan) -> None:
        assert self._bus is not None
        plan.status = "completed"
        self._save_plan(plan)
        await update_task_phase(self._task_id, "completed")
        await self._activity("phase", "All steps verified — task completed")

        summary = plan.final_summary
        if not summary:
            parts = [f"任务完成: {plan.requirement[:80]}"]
            for step in plan.steps:
                if step.status == "skipped":
                    continue
                changed = step.result.get("changed_files", {}) if step.result else {}
                if changed:
                    parts.append(f"  - {step.instruction[:50]}: 修改了 {len(changed)} 个文件")
                else:
                    parts.append(f"  - {step.instruction[:50]}: 完成")
            summary = "\n".join(parts)

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
                "summary": summary,
            },
            correlation_id=plan.correlation_id,
            metadata={"origin_channel_id": plan.origin_channel_id},
        ))

    async def _fail_plan(self, plan: TaskPlan, *, reason: str = "") -> None:
        assert self._bus is not None
        plan.status = "failed"
        plan.fail_reason = reason or plan.fail_reason
        self._save_plan(plan)
        await update_task_phase(self._task_id, "failed")

        lines = []
        if plan.fail_reason:
            lines.append(plan.fail_reason)
        for step in plan.steps:
            if step.status == "failed":
                result = step.result or {}
                findings = "; ".join(str(f) for f in result.get("verify_findings") or [])
                if result.get("verify_passed") is False:
                    error = f"review failed: {findings or result.get('verify_summary') or '未知原因'}"
                else:
                    error = result.get("error") or result.get("summary") or "未知错误"
                lines.append(f"  - {step.instruction[:50]}: {error}")
        summary = "任务失败:\n" + "\n".join(lines) if lines else "任务失败"
        await self._activity("error", f"Task failed: {plan.fail_reason or 'see step details'}"[:500])

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
                "summary": summary,
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

    async def _activity(self, kind: str, summary: str, *, worker: str | None = None) -> None:
        try:
            await db_activity.insert_activity(
                task_id=self._task_id, worker=worker or self.agent_id, kind=kind, summary=summary,
            )
        except Exception:
            logger.exception("failed to record planner activity for task %s", self._task_id)

    async def _refresh_project(self, plan: TaskPlan) -> None:
        """Read the task's project as it is now (the task may have moved).

        A failed read keeps what the plan last saw: the background is
        guidance, so the task goes on without the newest one.
        """
        try:
            project = await db_projects.get_task_project(self._task_id)
        except Exception:
            logger.warning("planner %s cannot read its task's project; keeping the last one read",
                           self.agent_id, exc_info=True)
            return
        if project is None:
            logger.warning("planner %s: task %s has no project to read; keeping the last one read",
                           self.agent_id, self._task_id)
            return
        plan.project_id = str(project.get("id") or plan.project_id)
        plan.project_name = str(project.get("name") or "")
        plan.project_description = str(project.get("description") or "")
        plan.project_background = str(project.get("background") or "")

    async def _note_background(self, background: str) -> None:
        """Note in the task's activity when the system prompt starts carrying, changes or drops the background."""
        previous, self._prompt_background = self._prompt_background, background
        if background == (previous or ""):
            return
        if not background:
            await self._activity("phase", "Project background removed")
        elif previous:
            await self._activity("phase", f"Project background changed ({len(background)} chars)")
        else:
            await self._activity("phase", f"Project background applied ({len(background)} chars)")

    async def _persist_steps(self, steps: list[PlanStep]) -> None:
        if not steps:
            return
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

    async def _persist_step(self, step: PlanStep) -> None:
        try:
            await db_steps.update_step_status(step.step_id, step.status, result=step.result)
        except Exception:
            logger.exception("failed to persist step %s", step.step_id)

    def _save_plan(self, plan: TaskPlan) -> None:
        path = self._plans_dir / f"{plan.plan_id}.json"
        data = plan.to_dict()
        data["correlation_id"] = plan.correlation_id
        data["origin_channel_id"] = plan.origin_channel_id
        data["work_dir"] = plan.work_dir
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
                        work_dir=data.get("work_dir", ""),
                        status=data.get("status", "executing"),
                        correlation_id=data.get("correlation_id", ""),
                        origin_channel_id=data.get("origin_channel_id", ""),
                        project_id=data.get("project_id", ""),
                        project_name=data.get("project_name", ""),
                        project_description=data.get("project_description", ""),
                        project_background=data.get("project_background", ""),
                        session_id=data.get("session_id"),
                        final_summary=data.get("final_summary", ""),
                        fail_reason=data.get("fail_reason", ""),
                    )
                    for sd in data.get("steps", []):
                        self._plan.steps.append(PlanStep(
                            step_id=sd["step_id"],
                            index=sd.get("index", 0),
                            instruction=sd.get("instruction", ""),
                            key=sd.get("key", ""),
                            status=sd.get("status", "pending"),
                            acceptance_criteria=sd.get("acceptance_criteria", []),
                            review=bool(sd.get("review", False)),
                            target_files=sd.get("target_files", []),
                            depends_on=sd.get("depends_on", []),
                            worker_id=sd.get("worker_id"),
                            reviewer_id=sd.get("reviewer_id"),
                            retries=int(sd.get("retries", 0)),
                            feedback=sd.get("feedback", []),
                        ))
                    break
            except (json.JSONDecodeError, KeyError):
                logger.warning("skipping corrupt plan file %s", path)
