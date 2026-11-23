"""Measured evidence constrains every decision path, including adjudication."""

from dataclasses import replace
from itertools import product

import pytest

from looper.agents.acceptance_checks import CommandCheck
from looper.agents.acceptance_gate import Gate, current_evidence, gate
from looper.agents.tiers.planner_protocol import (
    Decision, DecisionContext, PlanView, ProtocolError, ResultKind, parse_decision,
)
from tests.test_acceptance_checks_off import _main_modules
from tests.test_planner_agent import reply


BASELINE = "ecc2eedbf2ac4a1b44ff9078e634002c228458c1"
VERSION = ("head", "content")
CHECK = CommandCheck("c1", 1, "python verify.py")


def evidence(state="passed", **values):
    return {"step_id": "s1", "check_id": "c1", "attempt": 1, "run": 1,
            "state": state, "head_sha": "head", "content_sha": "content", **values}


def test_latest_measurement_is_selected_per_step_and_check_without_mutating_history():
    history = [evidence(run=8), evidence(attempt=2, run=1, state="failed"),
               evidence(attempt=2, run=2), evidence(step_id="s2", run=3),
               evidence(check_id="c2", run=2)]
    current = current_evidence(reversed(history))
    assert {(row["step_id"], row["check_id"]): (row["attempt"], row["run"])
            for row in current} == {("s1", "c1"): (2, 2), ("s2", "c1"): (1, 3), ("s1", "c2"): (1, 2)}
    assert history[1]["state"] == "failed"


@pytest.mark.parametrize(("checks", "rows", "passed", "failed", "errored", "stale"), [
    ((), (), True, (), (), ()),
    ((CHECK,), (evidence(),), True, (), (), ()),
    ((CHECK,), (), False, ("c1",), (), ()),
    ((CHECK,), (evidence("failed"),), False, ("c1",), (), ()),
    ((replace(CHECK, required=False),), (evidence("failed"),), True, (), (), ()),
    ((CHECK,), (evidence("error", error_kind="timeout"),), False, (), ("c1",), ()),
    ((CHECK,), (evidence("error", error_kind="budget"),), False, (), ("c1",), ()),
    ((CHECK,), (evidence("stale"),), False, (), (), ("c1",)),
    ((CHECK,), (evidence(head_sha="old"),), True, (), (), ()),  # committing the same content keeps the version
    ((CHECK,), (evidence(content_sha="old"),), False, (), (), ("c1",)),
    ((CHECK,), (evidence("running"),), False, ("c1",), (), ()),
    ((CHECK,), (evidence(protected_changed=["tests/verify.py"]),), False, ("c1",), (), ()),
    ((CHECK, replace(CHECK, id="advisory", required=False)),
     (evidence(), evidence("error", check_id="advisory")), True, (), (), ()),
])
def test_gate_table(checks, rows, passed, failed, errored, stale):
    result = gate(checks, rows, VERSION)
    assert (result.passed, result.failed, result.errored, result.stale) == (passed, failed, errored, stale)


def test_non_git_measurements_are_valid_on_the_same_content():
    assert gate((CHECK,), (evidence(head_sha=None, content_sha=None),), (None, None)).passed


@pytest.mark.parametrize(("state", "rechecks", "expected"), [
    ("passed", 0, ("accept", "review", "retry", "replan", "fail")),
    ("failed", 0, ("retry", "replan", "fail")),
    ("stale", 0, ("retry", "replan", "fail")),
    ("error", 0, ("retry", "recheck", "replan", "fail")),
    ("error", 1, ("retry", "recheck", "replan", "fail")),
    ("error", 2, ("retry", "replan", "fail")),
])
def test_allowed_decisions_are_intersected_with_the_gate(state, rechecks, expected):
    result = replace(gate((CHECK,), (evidence(state),), VERSION), rechecks_used=rechecks)
    context = DecisionContext("s1", ResultKind.WORK, True, False, 0, evidence=result)
    assert tuple(item.value for item in context.allowed()) == expected
    if state != "passed":
        assert tuple(item.value for item in context.offered(adjudicated_accept=True)) == expected
        assert not context.acceptable


def test_reviewer_and_advisor_cannot_override_failed_evidence_or_extend_worker_retries():
    result = gate((CHECK,), (evidence("failed"),), VERSION)
    context = DecisionContext("s1", ResultKind.REVIEW, True, True, 2, evidence=result, advise=True)
    assert context.offered(True) == (Decision.REPLAN, Decision.FAIL)
    assert not context.acceptable


def test_recheck_is_parsed_only_when_offered_and_needs_no_worker_feedback():
    result = gate((CHECK,), (evidence("error"),), VERSION)
    context = DecisionContext("s1", ResultKind.WORK, True, False, 2, evidence=result)
    view = PlanView(started={"s1": ()}, pending={})
    text = reply({"decision": "recheck", "reason": "Retry the measurement."})
    parsed = parse_decision(text, context, view)
    assert parsed.decision is Decision.RECHECK and parsed.feedback == ""
    for changed in (replace(context, evidence=None),
                    replace(context, evidence=replace(result, rechecks_used=2)),
                    replace(context, evidence=gate((CHECK,), (evidence("failed"),), VERSION))):
        with pytest.raises(ProtocolError):
            parse_decision(text, changed, view)


def test_failed_gate_cannot_finish_via_an_empty_replan():
    context = DecisionContext("s1", ResultKind.REVIEW, True, True, 0,
                              evidence=gate((CHECK,), (evidence("failed"),), VERSION))
    with pytest.raises(ProtocolError, match="cannot be accepted"):
        parse_decision(reply({"decision": "replan", "steps": []}), context, PlanView(started={"s1": ()}, pending={}))


def test_no_evidence_and_empty_checks_keep_fixed_baseline_decisions():
    old = _main_modules(BASELINE).protocol
    for kind, success, review, retries, advisor in product(("work", "review"), (False, True),
                                                        (False, True), (0, 2), (False, True)):
        before = old.DecisionContext("s1", old.ResultKind(kind), success, review, retries, advise=advisor)
        context = DecisionContext("s1", ResultKind(kind), success, review, retries, advise=advisor)
        for current in (context, replace(context, evidence=gate((), (), VERSION))):
            assert tuple(item.value for item in current.allowed()) == tuple(item.value for item in before.allowed())
            assert tuple(item.value for item in current.offered(True)) == tuple(item.value for item in before.offered(True))


@pytest.mark.parametrize(("state", "kind", "offered"), [
    ("error", "timeout", True), ("error", "budget", False),
])
def test_recheck_is_not_offered_for_a_spent_step_budget(state, kind, offered):
    current = (evidence(state, error_kind=kind),)
    context = DecisionContext("s1", ResultKind.WORK, True, False, 0, 3, evidence=gate((CHECK,), current, VERSION))
    assert (Decision.RECHECK in context.allowed()) is offered
