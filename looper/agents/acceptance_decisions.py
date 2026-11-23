"""Durable evidence decisions, separate from the Planner's dispatch state machine."""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, replace
from enum import Enum
from pathlib import Path

from looper.db import step_check_runs
from .acceptance_checks import AcceptanceChecksMode
from .acceptance_context import AcceptanceBaseline
from .acceptance_gate import current_evidence, gate
from . import acceptance_runner
from .attempts import FailureKind
from .tiers.planner_protocol import Decision, DecisionContext, ResultKind


logger = logging.getLogger(__name__)


class Rerun(str, Enum):
    RECHECK = "recheck"
    STALE = "stale"


@dataclass(frozen=True)
class EvidenceState:
    rechecks_used: int = 0
    stale_reruns_used: int = 0
    blocked: bool = False
    pending: Rerun | None = None
    after: dict[str, int] | None = None
    attempt: int = 0

    @classmethod
    def read(cls, step):
        data = (step.result or {}).get("acceptance") or {}
        return cls(**{**data, "pending": Rerun(data["pending"]) if data.get("pending") else None})

    def store(self, step):
        step.result = {**(step.result or {}), "acceptance": {
            "rechecks_used": self.rechecks_used, "stale_reruns_used": self.stale_reruns_used,
            "blocked": self.blocked, "pending": self.pending.value if self.pending else None,
            "after": self.after, "attempt": self.attempt}}


def _directory(plan, step) -> Path:
    return Path(step.workspace.work_dir if step.isolated else plan.work_dir)


async def _head(plan, step):
    from .tiers.worker import _verification_snapshot
    snapshot = await asyncio.to_thread(_verification_snapshot, str(_directory(plan, step)))
    if snapshot.get("error"):
        raise acceptance_runner.EvidenceUnavailable("current version unavailable")
    return snapshot.get("head"), snapshot.get("content_sha256")


async def _collect(agent, plan, step, state):
    baseline = AcceptanceBaseline.from_result(step.result, step.retries + 1)
    return await acceptance_runner.run_step_checks(task_id=agent._task_id, step_id=step.step_id,
        checks=step.checks, mode=agent._execution.acceptance_checks, attempt=step.retries + 1,
        runtime_root=agent._runtime_root, work_dir=_directory(plan, step),
        base_commit=baseline.head_sha if baseline else None,
        runtime_paths=(*agent._execution.link_paths, *(agent._execution.verification_paths or ())),
        **({"rerun_after": state.after} if state.pending else {}))


async def _schedule(agent, plan, step, state, current, reason):
    state = replace(state, pending=reason, after={row["check_id"]: row["run"] for row in current},
                    attempt=step.retries + 1,
                    rechecks_used=state.rechecks_used + (reason is Rerun.RECHECK),
                    stale_reruns_used=state.stale_reruns_used + (reason is Rerun.STALE))
    state.store(step)
    await agent._save(plan, step)  # Save the intent before any process: a restart resumes this round.
    return state


async def decision_context(agent, plan, step, context: DecisionContext) -> DecisionContext:
    """Collect on successful work, retain a blocked gate when disabled, and refresh stale evidence."""
    if not step.checks or context.checkpoint:
        return context
    enabled = agent._execution.acceptance_checks is not AcceptanceChecksMode.OFF
    state = EvidenceState.read(step)
    current = ()
    head = (None, None)
    try:
        if not enabled and not state.blocked:
            history = await step_check_runs.list_runs(agent._task_id, step.step_id)
            required = {check.id for check in step.checks if check.required}
            if not any(row["check_id"] in required and row["state"] != "passed"
                       for row in current_evidence(history)):
                return replace(context, evidence=None)
            state = replace(state, blocked=True)
        if state.pending and state.attempt != step.retries + 1:
            state = replace(state, pending=None, after=None)
        if enabled and ((context.result is ResultKind.WORK and context.succeeded) or state.pending):
            try:
                await _collect(agent, plan, step, state)
            except Exception:
                logger.warning("Could not collect acceptance evidence for %s", step.step_id, exc_info=True)
                await agent._activity("error", f"Acceptance evidence unavailable for step {step.label}")
        history = await step_check_runs.list_runs(agent._task_id, step.step_id)
        # Evidence from a previous Worker attempt cannot certify this attempt.
        current = current_evidence([row for row in history if row["attempt"] == step.retries + 1])
        head = await _head(plan, step)
        measured = gate(step.checks, current, head)
        if state.pending:
            state = replace(state, pending=None, after=None)
        outdated = any(row["state"] != "error" and (row["state"] == "stale"
                       or (row.get("head_sha"), row.get("content_sha")) != head) for row in current)
        if outdated and enabled:
            # Append a stale record, never rewrite a completed measurement. Already stale rows are reused.
            if any(row["state"] != "stale" for row in current):
                await acceptance_runner.mark_stale(task_id=agent._task_id, step_id=step.step_id,
                    checks=step.checks, attempt=step.retries + 1, current=current)
                current = current_evidence(await step_check_runs.list_runs(agent._task_id, step.step_id))
            if state.stale_reruns_used < 2:
                await _invalidate_review(agent, plan, step)
                context = replace(context, result=ResultKind.WORK, succeeded=(step.result or {}).get("status") == "completed")
                await agent._activity("acceptance_check", f"Evidence for {step.label} is stale; rerunning")
                state = await _schedule(agent, plan, step, state, current, Rerun.STALE)
                await _collect(agent, plan, step, state)
                state = replace(state, pending=None, after=None)
                current = current_evidence([row for row in await step_check_runs.list_runs(agent._task_id, step.step_id)
                                            if row["attempt"] == step.retries + 1])
                head = await _head(plan, step)
                measured = gate(step.checks, current, head)
        # A disabled project retains its earlier blocking decision, even if the version now matches.
        if not enabled and state.blocked:
            measured = replace(measured, passed=False)
    except Exception:
        logger.warning("Could not read acceptance evidence for %s", step.step_id, exc_info=True)
        await agent._activity("error", f"Acceptance evidence unavailable for step {step.label}")
        measured = replace(gate(step.checks, (), head), passed=False)
    state = replace(state, blocked=not measured.passed)
    state.store(step)
    await agent._save(plan, step)
    measured = replace(measured, rechecks_used=state.rechecks_used, stale_reruns_used=state.stale_reruns_used)
    return replace(context, evidence=measured)


def evidence_text(step, evidence) -> str:
    if evidence is None:
        return ""
    from .acceptance_evidence import render_evidence
    return render_evidence(step.acceptance_criteria, step.checks, evidence.current, evidence.head)


async def before_decision(agent, plan, step, context, reply):
    """Return a reply to apply, or None when a new evidence decision took over."""
    if context.evidence is None:
        return reply
    await agent._read_execution()
    if reply.decision is Decision.RECHECK:
        if Decision.RECHECK not in context.allowed():
            raise ValueError("recheck is not offered")
        state = EvidenceState.read(step)
        await _schedule(agent, plan, step, state, context.evidence.current, Rerun.RECHECK)
        await agent._decide_again(plan, step)
        return None
    if reply.decision in (Decision.ACCEPT, Decision.REVIEW) or (reply.decision is Decision.REPLAN and context.acceptable):
        fresh = await decision_context(agent, plan, step, context)
        if fresh.evidence is None:
            return reply
        if (not fresh.evidence.passed or fresh.evidence.head != context.evidence.head
                or fresh.evidence.stale_reruns_used != context.evidence.stale_reruns_used):
            await agent._decide_again(plan, step)
            return None
    if reply.decision is Decision.RETRY:
        from .acceptance_evidence import failed_feedback
        feedback = failed_feedback(step.acceptance_criteria, step.checks, context.evidence.current)
        if feedback:
            reply = replace(reply, feedback=reply.feedback + "\n\n" + feedback)
    if reply.decision is Decision.REPLAN and not context.evidence.passed:
        await agent._activity("acceptance_check", f"Replan replaces blocked checks for step {step.label}")
    return reply


def failure_kind(context: DecisionContext) -> FailureKind | None:
    """CHECK_FAILED when a required check was measured failed or errored on the version being decided, else the
    default mapping. A required check with no row (the Worker never reached collection) does not count."""
    gate = context.evidence
    if gate is None:
        return None
    measured = {row["check_id"] for row in gate.current}
    return FailureKind.CHECK_FAILED if measured & {*gate.failed, *gate.errored} else None


async def reviewer_payload(agent, plan, step):
    """Validate the measured version before dispatch, then render the same section for each seat."""
    if not step.checks:
        return {}
    context = DecisionContext(step.label, ResultKind.WORK, True, agent._needs_review(step), step.retries,
                              agent._execution.step_retries)
    before = EvidenceState.read(step).stale_reruns_used
    context = await decision_context(agent, plan, step, context)
    if context.evidence is None:
        return {}
    if not context.evidence.passed or context.evidence.stale_reruns_used != before:
        await agent._decide_again(plan, step)
        return None
    return {"evidence": evidence_text(step, context.evidence)}


async def fallback_context(agent, plan, step, context):
    if context.evidence is None:
        return context
    fresh = await decision_context(agent, plan, step, context)
    if fresh.evidence is None:
        return fresh
    if not fresh.evidence.passed:
        return replace(fresh, succeeded=False)
    if (fresh.evidence.head != context.evidence.head
            or fresh.evidence.stale_reruns_used != context.evidence.stale_reruns_used):
        await agent._decide_again(plan, step)
        return None
    return fresh


async def _invalidate_review(agent, plan, step):
    """A changed source invalidates every seat and the frozen snapshot of its old round."""
    if step.accept_when is not None:
        await agent._reset_reviews(plan, step)
    result = step.result or {}
    if "worker_head" in result:
        result = {**result, "head": result["worker_head"]}
    step.result = {key: value for key, value in result.items()
                   if key not in {"worker_head", "verify_passed", "verify_findings", "verify_summary",
                                  "verify_learnings", "role_reviews", "review_round"}}


async def resume_merge(agent, plan, step, *, decide: bool) -> bool:
    """An unfrozen interrupted merge must not commit source changed while the server was stopped."""
    from .step_workspaces import StepWorkspaceState
    if not step.checks or step.workspace.state is not StepWorkspaceState.MERGING or step.workspace.result_commit:
        return True
    before = EvidenceState.read(step).stale_reruns_used
    context = DecisionContext(step.label, ResultKind.WORK, True, agent._needs_review(step), step.retries,
                              agent._execution.step_retries)
    context = await decision_context(agent, plan, step, context)
    if context.evidence is None or (context.evidence.passed and context.evidence.stale_reruns_used == before):
        return True
    if decide:
        await agent._decide_again(plan, step)
    return False
