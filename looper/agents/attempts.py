"""Typed, best-effort observations of attempts and task runs.

This module never renders model input. Database failures are logged here and cannot
turn an otherwise usable task, dispatch or model result into a failure.
"""

from __future__ import annotations

import logging
from enum import StrEnum
from typing import TYPE_CHECKING

from ..db import llm_records, step_attempts, task_outcomes

if TYPE_CHECKING:
    from .tiers.worker import RunOutcome

logger = logging.getLogger(__name__)


class Trigger(StrEnum):
    FIRST = "first"
    REPLANNED = "replanned"
    RETRY = "retry"
    RESTART = "restart"
    REOPEN_LATE_REVIEW = "reopen_late_review"
    REOPEN_VERIFICATION = "reopen_verification"


class RunResult(StrEnum):
    COMPLETED = "completed"
    REPORTED_FAILED = "reported_failed"
    FILES_MISMATCH = "files_mismatch"
    CONTRACT = "contract"
    RUN_ERROR = "run_error"
    TIMEOUT = "timeout"
    RUN_CAP = "run_cap"
    CRASH = "crash"


class ReviewResult(StrEnum):
    NONE = "none"
    PASSED = "passed"
    NOT_PASSED = "not_passed"
    PASSED_BY_ADJUDICATION = "passed_by_adjudication"


class Decision(StrEnum):
    ACCEPT = "accept"
    RETRY = "retry"
    REPLAN = "replan"
    FAIL = "fail"
    SKIP = "skip"
    TASK_ENDED = "task_ended"


class FailureKind(StrEnum):
    REVIEW_REJECTED = "review_rejected"
    CONTRACT = "contract"
    FILES_MISMATCH = "files_mismatch"
    REPORTED_FAILED = "reported_failed"
    RUN_ERROR = "run_error"
    TIMEOUT = "timeout"
    RUN_CAP = "run_cap"
    CRASH = "crash"
    TASK_BUDGET = "task_budget"
    CHECK_FAILED = "check_failed"
    STOPPED = "stopped"


class RunFailure(StrEnum):
    TIMEOUT = "timeout"
    RUN_CAP = "run_cap"
    CRASH = "crash"


class TaskOutcome(StrEnum):
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    STOPPED = "stopped"


class StopCause(StrEnum):
    PLANNER_END = "planner_end"
    TASK_BUDGET = "task_budget"
    USER_STOP = "user_stop"
    USER_CANCEL = "user_cancel"
    MANAGER_CANCEL = "manager_cancel"
    RESTART = "restart"
    TEARDOWN = "teardown"
    ERROR = "error"


class TaskFailure(StrEnum):
    TASK_BUDGET = "task_budget"
    PLANNER_FAILED = "planner_failed"
    STEPS_FAILED = "steps_failed"
    ERROR = "error"


def run_result(outcome: RunOutcome, failure: RunFailure | None) -> RunResult:
    """A typed Looper termination marker survives fallback's report rendering."""
    from .tiers.worker import RunOutcome

    if failure is not None:
        return RunResult(failure.value)
    if outcome is RunOutcome.RUN_FAILED:
        return RunResult.RUN_ERROR
    return RunResult(outcome.value)


async def _observe(operation, *args, **kwargs):
    try:
        return await operation(*args, **kwargs)
    except Exception:
        logger.exception("Attempt observation failed: %s", operation.__name__)
        return None


async def open_attempt(task_id: str, step_id: str | None, step_key: str, run_no: int,
                       trigger: Trigger, *, pool=None) -> int | None:
    return await _observe(step_attempts.open_attempt, task_id, step_id, step_key, run_no, trigger, pool=pool)


async def current_attempt(task_id: str, step_key: str, run_no: int | None = None, *, pool=None) -> int | None:
    row = await _observe(step_attempts.latest_attempt, task_id, step_key, run_no, open_only=True, pool=pool)
    return row['id'] if row is not None else None


async def resolve_attempt(task_id: str, step_key: str, *, pool=None) -> int | None:
    row = await _observe(step_attempts.latest_attempt, task_id, step_key, pool=pool)
    return row['id'] if row is not None else None


async def dispatch_attempt(task_id: str, step_id: str | None, step_key: str, trigger: Trigger | None = None,
                           same_attempt: bool = False, *, pool=None) -> int | None:
    if same_attempt:
        return await current_attempt(task_id, step_key, pool=pool)
    run_no = await _observe(step_attempts.task_run_no, task_id, pool=pool)
    if run_no is None:
        return None
    if trigger in (None, Trigger.FIRST, Trigger.RESTART):
        restarted = await _observe(step_attempts.step_attempted_before_run, task_id, step_id, run_no, pool=pool)
        if trigger is None:
            previous = await _observe(step_attempts.latest_attempt, task_id, step_key, run_no, pool=pool)
            added = (previous is None and await _observe(
                step_attempts.step_added_during_run, task_id, step_id, run_no, pool=pool))
            trigger = (Trigger.RETRY if previous is not None and previous['decision'] == Decision.RETRY else
                       Trigger.REPLANNED if added else Trigger.RESTART if restarted else Trigger.FIRST)
        else:
            trigger = Trigger.RESTART if restarted else Trigger.FIRST
    return await open_attempt(task_id, step_id, step_key, run_no, trigger, pool=pool)


async def worker_started(attempt_id: int | None, record_id: int, profile_id: str | None, *, pool=None):
    if attempt_id is not None:
        await _observe(step_attempts.worker_started, attempt_id, record_id, profile_id, pool=pool)


async def link_record(attempt_id: int | None, record_id: int | None, *, pool=None):
    if attempt_id is not None and record_id is not None:
        await _observe(step_attempts.link_record, attempt_id, record_id, pool=pool)


async def link_advice(attempt_id: int | None, spawn_id: int, *, pool=None):
    if attempt_id is not None:
        await _observe(step_attempts.link_advice, attempt_id, spawn_id, pool=pool)


async def link_review_record(attempt_id: int | None, task_id: str, agent_id: str | None, *, pool=None):
    if attempt_id is not None and agent_id is not None:
        await _observe(step_attempts.link_review_record, attempt_id, task_id, agent_id, pool=pool)


async def worker_finished(attempt_id: int | None, outcome: RunOutcome, failure: RunFailure | None = None, *, pool=None):
    if attempt_id is not None:
        try:
            result = run_result(outcome, failure)
            await step_attempts.worker_finished(attempt_id, result, pool=pool)
        except Exception:
            logger.exception("Attempt observation failed: worker_finished")


async def review_finished(attempt_id: int | None, passed: bool, blocking_findings: int,
                          *, required: bool = True, ran: bool = True, pool=None):
    if attempt_id is not None:
        await _observe(step_attempts.review_finished, attempt_id, passed, blocking_findings, required=required, ran=ran, pool=pool)


async def decide_attempt(attempt_id: int | None, decision: Decision, record_id: int | None = None,
                         adjudicated: bool = False, *, pool=None):
    if attempt_id is not None:
        await _observe(step_attempts.decide_attempt, attempt_id, decision, record_id, adjudicated, pool=pool)


async def close_attempts(task_id: str, decision: Decision = Decision.TASK_ENDED,
                         failure_kind: FailureKind | None = None, step_key: str | None = None,
                         *, step_keys: list[str] | None = None, pool=None):
    await _observe(step_attempts.close_attempts, task_id, decision, failure_kind, step_key,
                   step_keys=step_keys, pool=pool)


async def record_outcomes(task_ids, outcome: TaskOutcome, stop_cause: StopCause,
                          failure_kind: TaskFailure | None = None, *, pool=None):
    if task_ids:
        await _observe(task_outcomes.record_outcomes, task_ids, outcome, stop_cause, failure_kind, pool=pool)


async def record_outcome(task_id: str, outcome: TaskOutcome, stop_cause: StopCause,
                         failure_kind: TaskFailure | None = None, *, pool=None):
    await record_outcomes([task_id], outcome, stop_cause, failure_kind, pool=pool)


async def count_contract_rejection(record_id: int | None, kind: str, *, pool=None):
    if record_id is not None:
        await _observe(llm_records.increment_contract_rejections, record_id, kind, pool=pool)
