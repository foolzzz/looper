"""Typed observation mappings; failures in observation never stop the task."""

import asyncio

import pytest

from looper.agents import attempts
from looper.agents.attempts import Decision, RunFailure, RunResult
from looper.agents.tiers.worker import RunOutcome


@pytest.mark.parametrize("outcome,failure,expected", [
    (RunOutcome.COMPLETED, None, RunResult.COMPLETED),
    (RunOutcome.REPORTED_FAILED, None, RunResult.REPORTED_FAILED),
    (RunOutcome.FILES_MISMATCH, None, RunResult.FILES_MISMATCH),
    (RunOutcome.CONTRACT, None, RunResult.CONTRACT),
    (RunOutcome.RUN_FAILED, None, RunResult.RUN_ERROR),
    (RunOutcome.RUN_FAILED, RunFailure.TIMEOUT, RunResult.TIMEOUT),
    (RunOutcome.RUN_FAILED, RunFailure.RUN_CAP, RunResult.RUN_CAP),
    (RunOutcome.RUN_FAILED, RunFailure.CRASH, RunResult.CRASH),
    (RunOutcome.CONTRACT, RunFailure.TIMEOUT, RunResult.TIMEOUT),
])
def test_run_result_uses_typed_failure_before_a_fallback_report(outcome, failure, expected):
    assert attempts.run_result(outcome, failure) is expected


@pytest.mark.parametrize("name,args", [
    ("open_attempt", ("task", "step", "s1", 1, attempts.Trigger.FIRST)),
    ("current_attempt", ("task", "s1", 1)),
    ("resolve_attempt", ("task", "s1")),
    ("dispatch_attempt", ("task", "step", "s1")),
    ("worker_started", (1, 2, "profile")),
    ("worker_finished", (1, RunOutcome.RUN_FAILED, RunFailure.CRASH)),
    ("link_record", (1, 2)),
    ("link_advice", (1, 2)),
    ("link_review_record", (1, "task", "reviewer-test")),
    ("review_finished", (1, False, 2)),
    ("decide_attempt", (1, Decision.FAIL)),
    ("close_attempts", ("task",)),
    ("record_outcome", ("task", attempts.TaskOutcome.FAILED, attempts.StopCause.ERROR)),
    ("record_outcomes", (["task"], attempts.TaskOutcome.STOPPED, attempts.StopCause.RESTART)),
    ("count_contract_rejection", (1, "PLAN")),
])
def test_observation_failures_are_logged_and_do_not_escape(name, args, caplog):
    class BrokenPool:
        async def execute(self, *args):
            raise RuntimeError("observation unavailable")

        fetchrow = execute
        fetchval = execute
        fetch = execute

    result = asyncio.run(getattr(attempts, name)(*args, pool=BrokenPool()))
    assert result is None
    assert "observation unavailable" in caplog.text
