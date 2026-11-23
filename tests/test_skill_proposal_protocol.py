"""Project opt-in and structural boundaries of Planner closing-turn proposals."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from looper.agents.tiers.planner_protocol import (
    DecisionContext, GateContext, PlanView, ProtocolError, ResultKind, WaitContext,
    parse_chat, parse_decision, parse_gate, parse_wait, reply_contract,
)
from looper.agents.user_gates import AgendaItem, AgendaStatus, GatePolicy, GateState, GateView
from looper.agents.waits import WaitState
from looper.agents.skill_proposals import Refusal

FINAL = {key: "done" for key in ("delivered", "how_to_use", "verification", "open_issues")}
DRAFT = {"action": "create", "name": "release-check", "description": "Before a release",
         "content": "Run the checks.", "why": "Repeated in this task and useful next time."}


def _text(data):
    return '```json\n' + json.dumps(data) + '\n```'


def _parse(kind, data, *, enabled=False, closing=True):
    if kind == "DECISION":
        return parse_decision(_text(data), DecisionContext("s1", ResultKind.WORK, True, False, 0,
                              last_open=closing), PlanView({"s1": ()}, {}), skill_proposals=enabled)
    if kind == "CHAT":
        view = PlanView({"g1": ()}, {} if closing else {"s2": ()},
                        gates={"g1": GateView(GateState.OPEN, ())}, gate_policy=GatePolicy.ALLOWED)
        return parse_chat(_text(data), view, skill_proposals=enabled)
    if kind == "GATE":
        agenda = (AgendaItem("q1", "Which?", status=AgendaStatus.RESOLVED, answer="A"),)
        return parse_gate(_text(data), GateContext("g1", agenda, last_open=closing),
                          PlanView({"g1": ()}, {}, gates={"g1": GateView(GateState.OPEN, agenda)},
                                   gate_policy=GatePolicy.ALLOWED), skill_proposals=enabled)
    return parse_wait(_text(data), WaitContext("w1", WaitState.TIMED_OUT, last_open=closing),
                      PlanView({"w1": (), **({"s2": ()} if not closing else {})}, {}),
                      skill_proposals=enabled)


SHAPES = {
    "DECISION": {"decision": "accept"},
    "CHAT": {"reply": "done", "action": "none", "gates": [{"id": "g1", "action": "cancel", "resolution": "done"}]},
    "GATE": {"reply": "done", "action": "none", "gate": "close",
             "agenda": [{"id": "q1", "question": "Which?", "status": "resolved", "answer": "A"}],
             "summary": {"decisions": ["A"], "assumptions": [], "open": []}},
    "WAIT": {"reply": "done", "action": "none", "wait": {"action": "proceed", "resolution": "ready"}},
}


@pytest.mark.parametrize("kind", SHAPES)
def test_off_preserves_unknown_key_refusal(kind):
    with pytest.raises(ProtocolError) as exc:
        _parse(kind, {**SHAPES[kind], "final": FINAL, "skill_proposals": [DRAFT]})
    assert str(exc.value) == f"the {kind} reply has unknown keys: skill_proposals"


def test_off_contract_matches_main_fixture():
    # Captured from main 6613380fa48d7e7d2a7103f53ad84621d9c5efb5, never a moving branch.
    expected = Path(__file__).with_name("fixtures").joinpath("planner_contract_main.txt").read_text()
    assert reply_contract() == expected
    assert reply_contract(skill_proposals=False) == expected
    assert reply_contract(skill_proposals=True).startswith(expected)
    assert "skill_proposals" in reply_contract(skill_proposals=True)


@pytest.mark.parametrize("kind", SHAPES)
def test_each_closing_shape_accepts_two_typed_proposals_only_when_on(kind):
    draft = {**DRAFT, "action": "update", "skill": "release-check", "evidence": [{"task": "t-one", "step": "s2"}]}
    result = _parse(kind, {**SHAPES[kind], "final": FINAL, "skill_proposals": [DRAFT, draft]}, enabled=True)
    assert [item.action.value for item in result.skill_proposals] == ["create", "update"]
    assert result.skill_proposals[0].to_dict() == DRAFT
    assert result.skill_proposals[1].to_dict() == draft


@pytest.mark.parametrize("kind", SHAPES)
def test_proposals_require_a_completing_reply_and_final(kind):
    with pytest.raises(ProtocolError, match="final"):
        _parse(kind, {**SHAPES[kind], "skill_proposals": [DRAFT]}, enabled=True)
    with pytest.raises(ProtocolError, match="completes the task|last open"):
        _parse(kind, {**SHAPES[kind], "skill_proposals": [DRAFT]}, enabled=True, closing=False)


@pytest.mark.parametrize("drafts", [pytest.param(drafts, id=case) for drafts, case in [
    ([DRAFT] * 3, "at most 2"), (None, "list"), (["draft"], "object"),
    ([{**DRAFT, "action": "delete"}], "create or update"),
    ([{**DRAFT, "action": "update"}], "skill"),
    ([{**DRAFT, "skill": "old"}], "only for an update"),
    ([{**DRAFT, "name": 1}], "name"), ([{**DRAFT, "name": "n" * 65}], "64"),
    ([{**DRAFT, "description": "d" * 1025}], "1024"),
    ([{**DRAFT, "content": "文" * 21846}], "65536"),
    ([{**DRAFT, "why": "w" * 501}], "500"),
    ([{**DRAFT, "evidence": "s1"}], "evidence"),
    ([{**DRAFT, "evidence": [{"task": "t-one", "record": 1}]}], "unknown keys"),
    ([{**DRAFT, "evidence": [{"task": 1}]}], "task"),
    ([{**DRAFT, "evidence": [{"task": "t-one"}] * 6}], "5"),
    ([{**DRAFT, "extra": "x"}], "unknown keys"),
]])
def test_proposal_shape_errors_preserve_the_final_summary(drafts):
    result = _parse("DECISION", {"decision": "accept", "final": FINAL, "skill_proposals": drafts}, enabled=True)
    assert result.final is not None
    assert result.skill_proposals[0].refusal is Refusal.SHAPE


@pytest.mark.parametrize("field", ["task", "step"])
@pytest.mark.parametrize("length", [64, 65])
def test_evidence_identifiers_are_bounded(field, length):
    draft = {**DRAFT, "evidence": [{"task": "t-one", field: "a" * length}]}
    result = _parse("DECISION", {"decision": "accept", "final": FINAL, "skill_proposals": [draft]}, enabled=True)
    if length == 64:
        assert result.skill_proposals[0].to_dict() == draft
    else:
        assert result.skill_proposals[0].refusal is Refusal.SHAPE


@pytest.mark.parametrize("kind", SHAPES)
def test_all_closing_shapes_keep_final_when_only_the_proposal_is_malformed(kind):
    result = _parse(kind, {**SHAPES[kind], "final": FINAL, "skill_proposals": [None]}, enabled=True)
    assert result.final is not None
    assert result.skill_proposals[0].refusal is Refusal.SHAPE


def test_content_checks_belong_to_propose():
    draft = {**DRAFT, "content": "---\npassword=synthetic-secret-value", "name": "Not a slug"}
    parsed = _parse("DECISION", {"decision": "accept", "final": FINAL, "skill_proposals": [draft]}, enabled=True)
    assert parsed.skill_proposals[0].content == draft["content"]
