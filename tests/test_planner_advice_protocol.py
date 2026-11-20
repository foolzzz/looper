"""The Planner's reply contract with the advisor on (the advisor RFC, sections 11 and 12): advise, proceed and
``advice_adoption`` in a DECISION, a step's difficulty and advise in a PLAN, the [ADVICE] reply, what a consult may
spend and the evidence a consult records (``looper/agents/tiers/planner_advice.py``)."""

from __future__ import annotations

import json
from dataclasses import replace
from typing import Any

import pytest

from looper.agents.tiers.planner_advice import AdviceBudget, Evidence, answer_lines, budget, ended_lines, material
from looper.agents.project_execution import AdvisorLimits
from looper.agents.tiers.planner_protocol import (
    AdviceAction, Decision, DecisionContext, PlanView, ProtocolError, ResultKind, parse_advice, parse_decision,
    parse_plan,
)

VIEW = PlanView(started={"s1": ()}, pending={"s2": ("s1",)}, advisor=True)
FAILED = DecisionContext("s1", ResultKind.WORK, False, False, 0, max_retries=2)
CHECKPOINT = DecisionContext("s2", ResultKind.WORK, False, False, 0, max_retries=2, checkpoint=True)
ADOPTION = {"adoption": "partial", "items": [{"ref": "q1", "use": "adopted", "steps": ["s1"], "why": "it fits"},
                                             {"ref": "s1", "use": "rejected", "why": "too slow"}]}


def _reply(**fields: Any) -> str:
    return f"```json\n{json.dumps(fields)}\n```"


def test_the_allowed_decisions_follow_the_checkpoint_the_offer_and_the_answer() -> None:
    assert CHECKPOINT.allowed() == (Decision.ADVISE,)
    assert replace(CHECKPOINT, answered=True).allowed() == (Decision.PROCEED, Decision.REPLAN, Decision.FAIL)
    assert replace(FAILED, advise=True).allowed() == (Decision.RETRY, Decision.ADVISE, Decision.REPLAN, Decision.FAIL)
    assert replace(FAILED, advise=True, answered=True).allowed() == (Decision.RETRY, Decision.REPLAN, Decision.FAIL)
    assert FAILED.allowed() == (Decision.RETRY, Decision.REPLAN, Decision.FAIL)
    # after a success, and with the retries used up (escalated too)
    succeeded = DecisionContext("s1", ResultKind.WORK, True, False, 0, max_retries=2, advise=True)
    assert Decision.ADVISE in succeeded.allowed() and Decision.ACCEPT in succeeded.allowed()
    used_up = replace(FAILED, retries=2, advise=True)
    assert used_up.allowed() == (Decision.ADVISE, Decision.REPLAN, Decision.FAIL)
    assert replace(used_up, escalate=True).allowed() == (Decision.ADVISE, Decision.REPLAN, Decision.FAIL)


def test_advise_carries_its_request_and_bindings_and_only_where_offered() -> None:
    request = {"v": "advice_request/1", "kind": "diagnose"}
    reply = parse_decision(_reply(decision="advise", advice_request=request,
                                  advice_bindings=[{"alias": "service A", "refers_to": "x"}]),
                           replace(FAILED, advise=True), VIEW)
    assert (reply.decision, reply.advice_request, reply.advice_bindings[0]["alias"]) == (
        Decision.ADVISE, request, "service A")
    with pytest.raises(ProtocolError, match="the advisor is not offered now"):
        parse_decision(_reply(decision="advise", advice_request=request), FAILED, VIEW)
    with pytest.raises(ProtocolError, match='advise needs "advice_request"'):
        parse_decision(_reply(decision="advise"), replace(FAILED, advise=True), VIEW)
    with pytest.raises(ProtocolError, match="only for advise"):
        parse_decision(_reply(decision="retry", feedback="x", advice_request=request), replace(FAILED, advise=True),
                       VIEW)
    with pytest.raises(ProtocolError, match="is critical: ask the advisor"):
        parse_decision(_reply(decision="retry", feedback="x"), CHECKPOINT, VIEW)


def test_the_decision_after_an_answer_must_say_what_it_takes_of_it() -> None:
    answered = replace(FAILED, answered=True)
    with pytest.raises(ProtocolError, match='must carry "advice_adoption"'):
        parse_decision(_reply(decision="retry", feedback="cache hot keys"), answered, VIEW)
    reply = parse_decision(_reply(decision="retry", feedback="cache hot keys", advice_adoption=ADOPTION), answered,
                           VIEW)
    assert reply.adoption["adoption"] == "partial" and [i["use"] for i in reply.adoption["items"]] == [
        "adopted", "rejected"]
    with pytest.raises(ProtocolError, match="only follows the advisor's answer"):
        parse_decision(_reply(decision="retry", feedback="x", advice_adoption=ADOPTION), FAILED, VIEW)
    for bad in ({"adoption": "maybe", "items": ADOPTION["items"]}, {"adoption": "adopted", "items": []},
                {"adoption": "adopted", "items": [{"ref": "q1", "use": "adopted"}]}):
        with pytest.raises(ProtocolError, match="advice_adoption"):
            parse_decision(_reply(decision="retry", feedback="x", advice_adoption=bad), answered, VIEW)


def test_after_a_checkpoints_answer_the_step_proceeds_or_its_plan_changes_in_place() -> None:
    answered = replace(CHECKPOINT, answered=True)
    proceed = parse_decision(_reply(decision="proceed", feedback="keep the old column", advice_adoption=ADOPTION),
                             answered, VIEW)
    assert (proceed.decision, proceed.feedback) == (Decision.PROCEED, "keep the old column")
    step = {"id": "s2", "instruction": "Add the column without a default first.", "acceptance_criteria": ["c"],
            "depends_on": ["s1"], "review": True, "difficulty": "critical"}
    replan = parse_decision(_reply(decision="replan", steps=[step], advice_adoption=ADOPTION), answered, VIEW)
    assert [(s.id, s.difficulty) for s in replan.steps] == [("s2", "critical")]
    with pytest.raises(ProtocolError, match="proceed only follows"):
        parse_decision(_reply(decision="proceed"), replace(FAILED, advise=True), VIEW)


def test_a_plan_may_ask_first_and_the_plan_after_the_answer_says_what_it_takes() -> None:
    from looper.agents.tiers.planner_protocol import parse_plan

    request = {"v": "advice_request/1", "kind": "design_choice"}
    asked = parse_plan(_reply(advise={"advice_request": request}), advisor=True, advise=True)
    assert (asked.steps, asked.advice_request) == ((), request)
    with pytest.raises(ProtocolError, match="unknown keys: advise"):
        parse_plan(_reply(advise={"advice_request": request}), advisor=True)  # not offered
    steps = [{"id": "s1", "instruction": "x", "acceptance_criteria": [], "depends_on": [], "review": False}]
    with pytest.raises(ProtocolError, match='must carry "advice_adoption"'):
        parse_plan(_reply(steps=steps), advisor=True, answered=True)
    assert parse_plan(_reply(steps=steps, advice_adoption=ADOPTION), advisor=True, answered=True).adoption[
        "adoption"] == "partial"


def test_a_step_says_its_difficulty_only_with_the_advisor_and_only_one_of_three() -> None:
    plan = {"steps": [{"id": "s1", "instruction": "x", "acceptance_criteria": [], "depends_on": [], "review": False,
                       "difficulty": "extreme"}]}
    with pytest.raises(ProtocolError, match="difficulty must be one of routine, hard, critical"):
        parse_plan(f"```json\n{json.dumps(plan)}\n```", advisor=True)
    with pytest.raises(ProtocolError, match="unknown keys: difficulty"):
        parse_plan(f"```json\n{json.dumps(plan)}\n```")


def test_an_advice_reply_answers_the_advisors_questions_or_withdraws() -> None:
    reply = parse_advice(_reply(advice="supplement", answers=[{"needs_info_id": "n1", "answer": "About 70%."}]),
                         ["n1", "n2"])
    assert (reply.action, reply.answers) == (AdviceAction.SUPPLEMENT, ({"needs_info_id": "n1", "answer": "About 70%."},))
    assert parse_advice(_reply(advice="withdraw", reason="the plan changed"), ["n1"]).reason == "the plan changed"
    for bad in (_reply(advice="supplement", answers=[{"needs_info_id": "n9", "answer": "x"}]),
                _reply(advice="supplement", answers=[]), _reply(advice="withdraw"), _reply(advice="maybe")):
        with pytest.raises(ProtocolError):
            parse_advice(bad, ["n1"])


ADVICE = {"status": "answered",
          "answers": [{"question_id": "q1", "conclusion": "Cache hot keys.", "basis": ["reads repeat"],
                       "confidence": "med", "assumptions": []}],
          "options": [{"id": "o1", "verdict": "prefer", "why": "simplest"}],
          "next_steps": [{"id": "s1", "action": "cache for 1 s", "verify": "P99 < 50 ms"}], "risks": ["stale reads"]}


def test_the_planner_reads_the_answer_point_by_point_and_a_failure_by_its_kind() -> None:
    assert answer_lines(ADVICE)[:2] == ["- q1 (confidence med): Cache hot keys.", "  basis: reads repeat"]
    assert ended_lines("rate_limited")[0] == ("The consult gave no answer: rate_limited, the advisor's model is "
                                              "rate-limited or out of quota.")


def test_the_evidence_changes_with_a_new_attempt_a_changed_step_or_the_users_word() -> None:
    base = Evidence("step-a", "r1", 0, attempt=1, worker="worker-1", worker_outcome="failed", instruction="i1")
    assert base.fingerprint() == Evidence("step-a", "r1", 0, attempt=1, worker="worker-1", worker_outcome="failed",
                                          instruction="i1").fingerprint()
    for changed in (replace(base, attempt=2), replace(base, worker="worker-2"), replace(base, instruction="i2"),
                    replace(base, requirement="r2"), replace(base, gates_answered=1)):
        assert changed.fingerprint() != base.fingerprint()


def test_the_budget_offered_is_what_is_left_of_each_limit_set() -> None:
    unlimited = budget(AdvisorLimits(), step_consults=9, task_consults=99, task_spent=49.0)
    assert unlimited == AdviceBudget(1.0) and unlimited.open  # the first phase sets no limit: only $1 a consult
    assert unlimited.line() == "This consult may spend up to $1."
    limits = AdvisorLimits(consult_budget_usd=1.0, budget_usd=5.0, consults_per_task=10, consults_per_step=2)
    assert budget(limits, step_consults=1, task_consults=3, task_spent=4.5) == AdviceBudget(0.5, 1, 7, 0.5)
    assert not budget(limits, step_consults=2, task_consults=3, task_spent=0).open
    assert not budget(limits, step_consults=0, task_consults=0, task_spent=4.95).open  # under 0.10 left
    text = material(instruction="Rewrite the loop.", criteria=["P99 < 50 ms"], report=["Worker: " + "x" * 9_000])
    assert len(text) == 8_000 and text.startswith("The step's instruction:")
