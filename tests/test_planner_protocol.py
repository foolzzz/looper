"""The Planner's reply contract: each reply is validated strictly against the plan it changes."""

from __future__ import annotations

import json
from typing import Any

import pytest

from looper.agents.tiers.planner_protocol import (
    MAX_STEPS,
    Decision,
    DecisionContext,
    PlanView,
    ProtocolError,
    ResultKind,
    extract_reply,
    parse_chat,
    parse_decision,
    parse_plan,
    parse_supervise,
)
from tests.test_planner_agent import chat, decide, plan, reply, step


def _error(parse: Any, *args: Any, **kwargs: Any) -> str:
    with pytest.raises(ProtocolError) as excinfo:
        parse(*args, **kwargs)
    return str(excinfo.value)


class TestReplyBlock:
    @pytest.mark.parametrize(
        ("text", "error"),
        [
            ("just prose", "no ```json fenced block"),
            ('```\n{"steps": []}\n```', "no ```json fenced block"),
            ('```json\n{}\n```\n```json\n{}\n```', "has 2 ```json blocks"),
            ('```json\n{}\n```\nthanks', "text after it"),
            ("```json\n{steps: []}\n```", "not valid JSON"),
            ("```json\n[1, 2]\n```", "one JSON object"),
            ('```json\n{"a": 1} {"b": 2}\n```', "hold one JSON value and close with ```"),
            ('```json\n{"a": 1}\n', "hold one JSON value and close with ```"),
        ],
    )
    def test_bad_blocks(self, text: str, error: str) -> None:
        assert error in _error(extract_reply, text)

    def test_string_values_may_quote_code_fences(self) -> None:
        """An instruction that shows code in a fence does not end the block (seen with real Claude)."""
        instruction = "Append:\n\n```python\ndef mul(a, b):\n    return a * b\n```\nThen run the tests."
        parsed = parse_plan("One step.\n\n```json\n" + json.dumps({"steps": [
            {**step("s1"), "instruction": instruction},
        ]}) + "\n```")
        assert parsed.steps[0].instruction == instruction


class TestPlan:
    def test_valid_plan(self) -> None:
        parsed = parse_plan(plan(step("s1"), step("s2", "s1", review=True)))
        assert [(s.id, s.depends_on, s.review) for s in parsed.steps] == [("s1", (), False), ("s2", ("s1",), True)]
        assert parsed.steps[1].acceptance_criteria == ("s2 works",)

    @pytest.mark.parametrize(
        ("steps", "error"),
        [
            ([], "at least one step"),
            ([step("s1"), step("s1")], "used twice"),
            ([step("s1", "s9")], "unknown step 's9'"),
            ([step("s1", "s1")], "depends on itself"),
            ([step("a", "b"), step("b", "c"), step("c", "a")], "cycle: a -> b -> c -> a"),
            ([step(f"s{i}") for i in range(MAX_STEPS + 1)], f"at most {MAX_STEPS}"),
            ([{**step("s1"), "notes": "x"}], "unknown keys: notes"),
            ([{k: v for k, v in step("s1").items() if k != "review"}], "missing review"),
            ([{**step("s1"), "id": "has space"}], "short label"),
            ([{**step("s1"), "review": "yes"}], "true or false"),
            ([{**step("s1"), "acceptance_criteria": "works"}], "list of non-empty strings"),
            ([{**step("s1"), "instruction": " "}], "non-empty string"),
            ([step("s1"), step("s2", "s1", "s1")], "lists a step twice"),
        ],
    )
    def test_invalid_plans(self, steps: list[dict[str, Any]], error: str) -> None:
        assert error in _error(parse_plan, plan(*steps))

    def test_unknown_top_level_key(self) -> None:
        assert "unknown keys: reason" in _error(parse_plan, reply({"steps": [step("s1")], "reason": "x"}))


def _context(
    result: ResultKind = ResultKind.WORK, *, succeeded: bool = True, review: bool = False, retries: int = 0,
) -> DecisionContext:
    return DecisionContext(step="s1", result=result, succeeded=succeeded, review_required=review, retries=retries)


# s1 has its result; s2 has not started; s0 is done.
VIEW = PlanView(started={"s0": (), "s1": ("s0",)}, pending={"s2": ("s1",)}, retired=frozenset({"old"}), spent=1)


class TestDecision:
    @pytest.mark.parametrize(
        ("context", "allowed"),
        [
            (_context(), "accept review retry replan fail"),
            (_context(review=True), "review retry replan fail"),
            (_context(succeeded=False), "retry replan fail"),
            (_context(ResultKind.REVIEW, review=True), "accept retry replan fail"),
            (_context(ResultKind.REVIEW, succeeded=False, review=True), "retry replan fail"),
            (_context(retries=2), "accept review replan fail"),
        ],
    )
    def test_allowed_decisions(self, context: DecisionContext, allowed: str) -> None:
        assert " ".join(d.value for d in context.allowed()) == allowed
        for decision in Decision:
            fields = {"feedback": "f"} if decision is Decision.RETRY else {}
            fields |= {"reason": "r"} if decision is Decision.FAIL else {}
            fields |= {"steps": [step("s3")]} if decision is Decision.REPLAN else {}
            text = decide(decision.value, **fields)
            if decision.value in allowed.split():
                assert parse_decision(text, context, VIEW).decision is decision
            else:
                assert f"{decision.value} is not allowed" in _error(parse_decision, text, context, VIEW)

    @pytest.mark.parametrize(
        ("context", "error"),
        [
            (_context(review=True), "needs a passing review before it can be accepted"),
            (_context(succeeded=False), "did not succeed, so it cannot be accepted"),
            (_context(ResultKind.REVIEW, succeeded=False), "did not succeed, so it cannot be accepted"),
        ],
    )
    def test_refused_accept_says_why(self, context: DecisionContext, error: str) -> None:
        assert error in _error(parse_decision, decide("accept"), context, VIEW)

    @pytest.mark.parametrize(
        ("text", "error"),
        [
            (decide("retry"), "feedback is required"),
            (decide("fail"), "reason is required"),
            (decide("accept", feedback="x"), "feedback is only for a retry"),
            (decide("accept", steps=[]), "steps is only for a replan"),
            (decide("approve"), "decision must be one of accept, review, retry, replan, fail"),
            (decide("accept", summary=3), "summary must be a string"),
            (decide("accept", extra=1), "unknown keys: extra"),
        ],
    )
    def test_invalid_fields(self, text: str, error: str) -> None:
        assert error in _error(parse_decision, text, _context(), VIEW)


class TestReplan:
    def _replan(self, *steps: dict[str, Any], context: DecisionContext | None = None) -> Any:
        return parse_decision(decide("replan", steps=list(steps)), context or _context(), VIEW)

    @pytest.mark.parametrize(
        ("steps", "error"),
        [
            ([step("s1")], "step s1 has started; started steps cannot change"),
            ([step("s0")], "step s0 has started"),
            ([step("old")], "belongs to a dropped step"),
            ([step("s3", "old")], "depends on old, which is dropped"),
            ([step("s3", "s2")], "unknown step 's2'"),  # s2 is replaced by this list, which drops it
            ([step(f"n{i}") for i in range(MAX_STEPS - 2)], "would have 9 steps (dropped steps that ran count too)"),
        ],
    )
    def test_invalid_replans(self, steps: list[dict[str, Any]], error: str) -> None:
        assert error in _error(self._replan, *steps)

    def test_a_step_that_cannot_be_accepted_is_dropped(self) -> None:
        failed = _context(succeeded=False)
        assert "depends on s1, which is dropped" in _error(self._replan, step("s3", "s1"), context=failed)
        assert "must list the steps that replace it" in _error(self._replan, context=failed)
        assert [s.id for s in self._replan(step("s1b", "s0"), context=failed).steps] == ["s1b"]
        assert self._replan().steps == ()  # after an acceptable step, an empty replan finishes the plan


RUNNING = PlanView(started={"s1": ()}, pending={"s2": ("s1",)}, running=frozenset({"s1"}))


class TestChat:
    @pytest.mark.parametrize(
        ("text", "error"),
        [
            (chat("ok", "forward", step="s2", text="x"), "step 's2' has no running Worker (running: s1)"),
            (chat("ok", "forward", step="s1"), "text is required"),
            (chat("ok", text="x"), "step and text are only for action forward"),
            (chat("ok", "add_steps", steps=[step("s2")]), "step id 's2' is already used"),
            (chat("ok", "add_steps", steps=[]), "at least one step"),
            (chat("ok", steps=[step("s3")]), "steps is only for action add_steps"),
            (chat(""), "reply is required"),
            (chat("ok", "stop"), "action must be one of none, forward, add_steps"),
            (reply({"reply": "ok"}), "action must be one of"),
        ],
    )
    def test_invalid_chat_replies(self, text: str, error: str) -> None:
        assert error in _error(parse_chat, text, RUNNING)


def supervise(*reviews: dict[str, Any]) -> str:
    return reply({"reviews": list(reviews)})


RUNNING2 = PlanView(
    started={"s1": (), "s2": ()}, pending={}, running=frozenset({"s1", "s2"}),
)


class TestSupervise:
    def test_leave_nudge_kill(self) -> None:
        parsed = parse_supervise(
            supervise(
                {"step": "s1", "action": "nudge", "text": "focus on the API"},
                {"step": "s2", "action": "kill", "reason": "wrong file"},
            ),
            RUNNING2.running,
        )
        assert [(r.step, r.action.value, r.text, r.reason) for r in parsed.reviews] == [
            ("s1", "nudge", "focus on the API", ""),
            ("s2", "kill", "", "wrong file"),
        ]

    @pytest.mark.parametrize(
        ("text", "error"),
        [
            (supervise({"step": "s9", "action": "leave"}), "has no running Worker (running: s1, s2)"),
            (supervise({"step": "s1", "action": "nudge"}), "text is required"),
            (supervise({"step": "s1", "action": "kill"}), "reason is required"),
            (supervise({"step": "s1", "action": "leave", "text": "x"}), "text is only for action nudge"),
            (supervise({"step": "s1", "action": "nudge", "text": "x", "reason": "y"}),
             "reason is only for action kill"),
            (supervise({"step": "s1", "action": "stop"}), "action must be one of leave, nudge, kill"),
            (supervise({"step": "s1", "action": "leave"}, {"step": "s1", "action": "leave"}),
             "is reviewed twice"),
            (reply({"reviews": {}}), "reviews must be a list"),
            (reply({"steps": []}), "unknown keys"),
        ],
    )
    def test_invalid_supervise_replies(self, text: str, error: str) -> None:
        assert error in _error(parse_supervise, text, RUNNING2.running)


def test_contract_examples_parse() -> None:
    """The JSON examples the contract shows Claude are valid replies themselves."""
    from looper.agents.tiers.planner_protocol import REPLY_CONTRACT

    examples = [json.loads(line) for line in REPLY_CONTRACT.splitlines() if line.startswith("{")]
    assert len(examples) == 4
    plan_example, decision_example, chat_example, supervise_example = examples
    parse_plan(reply(plan_example))
    parse_decision(reply(decision_example), _context(), VIEW)
    parse_chat(reply(chat_example), RUNNING)
    parse_supervise(reply(supervise_example), RUNNING.running)
