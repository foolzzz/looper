"""The reply contract's user gates: a gate step, a [GATE] reply and a DECISION or CHAT reply's ``gates``, each checked
against the plan it answers; and the contract, which says nothing of gates where a task may not open them."""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

import pytest

from looper.agents.tiers.planner_protocol import (
    ChatAction,
    Decision,
    DecisionContext,
    GateContext,
    PlanView,
    ProtocolError,
    ResultKind,
    parse_chat,
    parse_decision,
    parse_gate,
    parse_plan,
    reply_contract,
)
from looper.agents.user_gates import (
    AgendaItem,
    AgendaStatus,
    GateAction,
    GatePolicy,
    GateState,
    GateSummary,
    GateVerdict,
    GateView,
    conclusion_text,
    gate_policy,
    numbered,
)


def _reply(data: dict[str, Any]) -> str:
    return "Thinking it over.\n\n```json\n" + json.dumps(data) + "\n```"


def _step(step_id: str, *depends_on: str) -> dict[str, Any]:
    return {"id": step_id, "instruction": f"do {step_id}", "acceptance_criteria": [f"{step_id} works"],
            "depends_on": list(depends_on), "review": False}


def _gate(step_id: str = "g1", *depends_on: str, **extra: Any) -> dict[str, Any]:
    return {"id": step_id, "kind": "gate", "topic": "storage", "depends_on": list(depends_on),
            "agenda": [{"question": "Which database?", "options": ["PostgreSQL", "SQLite"]},
                       {"question": "How long is history kept?"}], **extra}


AGENDA = (AgendaItem("q1", "Which database?", ("PostgreSQL", "SQLite")), AgendaItem("q2", "How long is history kept?"))


def _item(item_id: str | None, status: str, answer: str | None = None, question: str = "?") -> dict[str, Any]:
    return {**({"id": item_id} if item_id else {}), "question": question, "status": status,
            **({"answer": answer} if answer is not None else {})}


# A plan whose gate g1 is open while s1 runs; s2 waits for g1.
OPEN = PlanView(started={"s1": (), "g1": ()}, pending={"s2": ("g1",)}, running=frozenset({"s1"}),
                gates={"g1": GateView(GateState.OPEN, AGENDA)}, gate_policy=GatePolicy.ALLOWED)


def test_a_gate_step_parses_with_its_agenda_numbered_and_no_criteria_or_review() -> None:
    plan = parse_plan(_reply({"steps": [_step("s1"), _gate("g1"), _step("s2", "g1")]}),
                      gate_policy=GatePolicy.ALLOWED)

    gate = plan.steps[1]
    assert gate.gate is not None and gate.instruction == "storage"
    assert (gate.acceptance_criteria, gate.review, gate.depends_on) == ((), False, ())
    assert [(i.id, i.question, i.options, i.status) for i in gate.gate.agenda] == [
        ("q1", "Which database?", ("PostgreSQL", "SQLite"), AgendaStatus.OPEN),
        ("q2", "How long is history kept?", (), AgendaStatus.OPEN)]
    assert plan.steps[0].gate is None and plan.steps[2].depends_on == ("g1",)


@pytest.mark.parametrize(("policy", "reason"), [
    (GatePolicy.OFF, "this project does not let you open gates (user_gates is off)"),
    (GatePolicy.UNATTENDED, "this task runs unattended (a schedule started it), so it cannot wait for the user"),
])
def test_a_gate_is_refused_where_the_task_may_not_open_one_with_what_to_do_instead(
    policy: GatePolicy, reason: str,
) -> None:
    with pytest.raises(ProtocolError) as refused:
        parse_plan(_reply({"steps": [_gate()]}), gate_policy=policy)

    assert str(refused.value) == f"step g1: {reason}: decide yourself, write your assumption into the instructions " \
                                 "of the steps that rely on it, and list your assumptions in the final summary's " \
                                 "open_issues"


@pytest.mark.parametrize(("change", "error"), [
    ({"review": True}, "steps[0] is a gate, which has only id, kind, topic, agenda, depends_on and findings; not "
                       "review"),
    ({"role": "tester"}, "not role"),
    ({"agenda": []}, "steps[0].agenda must list at least one question"),
    ({"agenda": [{"question": "?", "status": "open"}]}, "steps[0].agenda[0] has unknown keys: status"),
    ({"agenda": [{"question": "?", "options": ["a"] * 7}]}, "steps[0].agenda[0].options has 7 options; at most 6"),
    ({"topic": ""}, "steps[0].topic must be a non-empty string"),
    ({"kind": "review"}, 'steps[0].kind must be "gate" (leave it out for a work step)'),
])
def test_a_gate_step_of_the_wrong_shape_is_refused(change: dict[str, Any], error: str) -> None:
    with pytest.raises(ProtocolError, match=re.escape(error)):
        parse_plan(_reply({"steps": [{**_gate(), **change}]}), gate_policy=GatePolicy.ALLOWED)


def test_an_open_gate_is_started_so_a_replan_keeps_it_and_cannot_change_it() -> None:
    # A replan that leaves it out keeps it; s2 may still depend on it.
    kept = parse_chat(_reply({"reply": "ok", "action": "replan", "steps": [_step("s3", "g1")]}), OPEN)
    assert [s.id for s in kept.steps] == ["s3"]
    with pytest.raises(ProtocolError, match='gate g1 is open: a replan keeps it; change it or cancel it with "gates"'):
        parse_chat(_reply({"reply": "ok", "action": "replan", "steps": [_gate("g1")]}), OPEN)


def test_a_pending_id_keeps_its_kind() -> None:
    plan = PlanView(started={}, pending={"g1": (), "s1": ()}, gates={"g1": GateView(GateState.PENDING, AGENDA)},
                    gate_policy=GatePolicy.ALLOWED)
    with pytest.raises(ProtocolError, match="step g1 is a gate; use a new id for a step of the other kind"):
        parse_chat(_reply({"reply": "ok", "action": "replan", "steps": [_step("g1")]}), plan)
    with pytest.raises(ProtocolError, match="step s1 is a work step; use a new id"):
        parse_chat(_reply({"reply": "ok", "action": "replan", "steps": [_gate("s1")]}), plan)


def _gate_reply(gate: str = "continue", agenda: list[dict[str, Any]] | None = None, **extra: Any) -> str:
    agenda = agenda if agenda is not None else [_item("q1", "open"), _item("q2", "resolved", "90 days")]
    return _reply({"reply": "Noted.", "gate": gate, "agenda": agenda, "action": "none", **extra})


def test_a_gate_reply_lists_every_item_by_id_and_new_items_get_the_next_ids() -> None:
    reply = parse_gate(_gate_reply(agenda=[_item("q2", "resolved", "90 days"), _item("q1", "dropped"),
                                           _item(None, "open", question="Migrate the old rows?")]),
                       GateContext("g1", AGENDA), OPEN)

    assert reply.verdict is GateVerdict.CONTINUE and reply.summary is None
    assert [(i.id, i.status, i.answer) for i in reply.agenda] == [
        ("q2", AgendaStatus.RESOLVED, "90 days"), ("q1", AgendaStatus.DROPPED, ""), ("q3", AgendaStatus.OPEN, "")]
    assert reply.chat.action is ChatAction.NONE and reply.chat.reply == "Noted."


@pytest.mark.parametrize(("agenda", "error"), [
    ([_item("q1", "open")], "agenda must list q2; mark an item dropped to remove it"),
    ([_item("q1", "open"), _item("q9", "open")], "agenda[1].id 'q9' is not an item of this agenda"),
    ([_item("q1", "open"), _item("q1", "open"), _item("q2", "open")], "agenda[1]: item q1 is listed twice"),
    ([_item("q1", "resolved"), _item("q2", "open")], "agenda[0].answer must be a non-empty string"),
    ([_item("q1", "maybe"), _item("q2", "open")], "agenda[0].status must be open, resolved or dropped"),
])
def test_a_gate_reply_with_a_wrong_agenda_is_refused(agenda: list[dict[str, Any]], error: str) -> None:
    with pytest.raises(ProtocolError, match="^" + re.escape(error)):
        parse_gate(_gate_reply(agenda=agenda), GateContext("g1", AGENDA), OPEN)


SUMMARY = {"decisions": ["PostgreSQL"], "assumptions": [], "open": []}


def test_closing_needs_a_summary_and_every_item_resolved_or_dropped() -> None:
    settled = [_item("q1", "resolved", "PostgreSQL"), _item("q2", "dropped")]
    with pytest.raises(ProtocolError, match='closing the gate needs "summary"'):
        parse_gate(_gate_reply("close", settled), GateContext("g1", AGENDA), OPEN)
    with pytest.raises(ProtocolError, match="agenda items q1 are still open: resolve or drop each before closing"):
        parse_gate(_gate_reply("close", [_item("q1", "open"), _item("q2", "dropped")], summary=SUMMARY),
                   GateContext("g1", AGENDA), OPEN)
    with pytest.raises(ProtocolError, match='summary is only for "gate": "close"'):
        parse_gate(_gate_reply("continue", settled, summary=SUMMARY), GateContext("g1", AGENDA), OPEN)

    reply = parse_gate(_gate_reply("close", settled, summary=SUMMARY), GateContext("g1", AGENDA), OPEN)
    assert reply.verdict is GateVerdict.CLOSE and reply.summary == GateSummary(("PostgreSQL",))


def test_when_the_user_ends_the_discussion_the_reply_closes_with_an_assumption_for_each_open_item() -> None:
    ended = GateContext("g1", AGENDA, forced=True)
    with pytest.raises(ProtocolError, match='the user ended the discussion, so gate must be "close"'):
        parse_gate(_gate_reply("continue"), ended, OPEN)
    with pytest.raises(ProtocolError, match="agenda items q1 are still open: state your assumption for each"):
        parse_gate(_gate_reply("close", summary=SUMMARY), ended, OPEN)

    reply = parse_gate(_gate_reply("close", summary={**SUMMARY, "assumptions": ["SQLite is fine for now"]}), ended,
                       OPEN)
    assert reply.summary is not None and reply.summary.assumptions == ("SQLite is fine for now",)


FINAL = {"delivered": "x", "how_to_use": "y", "verification": "z", "open_issues": "none"}
LAST = PlanView(started={"g1": ()}, pending={}, gates={"g1": GateView(GateState.DISCUSSING, AGENDA)},
                gate_policy=GatePolicy.ALLOWED)


def test_closing_the_last_open_gate_without_new_steps_completes_the_task_so_it_carries_final() -> None:
    settled = [_item("q1", "resolved", "PostgreSQL"), _item("q2", "dropped")]
    last = GateContext("g1", AGENDA, last_open=True)
    with pytest.raises(ProtocolError, match='closing this gate without adding steps completes the task'):
        parse_gate(_gate_reply("close", settled, summary=SUMMARY), last, LAST)

    assert parse_gate(_gate_reply("close", settled, summary=SUMMARY, final=FINAL), last, LAST).final is not None
    # Steps added in the same reply follow the gate: nothing completes yet.
    added = parse_gate(_reply({"reply": "Building it.", "gate": "close", "agenda": settled, "summary": SUMMARY,
                               "action": "add_steps", "steps": [_step("s9", "g1")]}), last, LAST)
    assert added.final is None and [s.id for s in added.chat.steps] == ["s9"]


def _decision(context: DecisionContext, plan: PlanView, **data: Any) -> Any:
    return parse_decision(_reply({"decision": "accept", "summary": "done", **data}), context, plan)


DONE = DecisionContext(step="s1", result=ResultKind.WORK, succeeded=True, review_required=False, retries=0)


def test_a_decision_updates_an_open_gate_with_a_note_for_the_user_and_cancels_another() -> None:
    plan = PlanView(started={"s1": (), "g1": ()}, pending={"g2": (), "s2": ("g1",)}, running=frozenset({"s1"}),
                    gates={"g1": GateView(GateState.OPEN, AGENDA), "g2": GateView(GateState.PENDING, AGENDA)},
                    gate_policy=GatePolicy.ALLOWED)
    reply = _decision(DONE, plan, gates=[
        {"id": "g1", "action": "update", "findings": "s1 found PostgreSQL 15", "note": "q1 may be moot",
         "agenda": [_item("q1", "dropped"), _item("q2", "open")]},
        {"id": "g2", "action": "cancel", "resolution": "config/retention.yaml says 90 days"}])

    update, cancel = reply.gates
    assert (update.action, update.findings, update.note) == (GateAction.UPDATE, "s1 found PostgreSQL 15",
                                                             "q1 may be moot")
    assert [i.status for i in update.agenda] == [AgendaStatus.DROPPED, AgendaStatus.OPEN]
    assert (cancel.action, cancel.resolution) == (GateAction.CANCEL, "config/retention.yaml says 90 days")


@pytest.mark.parametrize(("changes", "error"), [
    ([{"id": "g1", "action": "update", "findings": "x"}], "gates[0].note must be a non-empty string"),
    ([{"id": "g1", "action": "update", "note": "n"}], "gates[0]: update needs at least one of topic, agenda and "
                                                      "findings"),
    ([{"id": "g1", "action": "cancel"}], "gates[0].resolution must be a non-empty string"),
    ([{"id": "g1", "action": "cancel", "resolution": "r", "note": "n"}], "gates[0]: cancel takes only resolution"),
    ([{"id": "g3", "action": "cancel", "resolution": "r"}], "gates[0].id 'g3' is not a gate that is still open"),
    ([{"id": "g1", "action": "close"}], 'gates[0].action must be "update" or "cancel"'),
    ([{"id": "g1", "action": "cancel", "resolution": "r"}] * 2, "gates[1]: gate g1 is changed twice"),
])
def test_a_wrong_gate_change_is_refused(changes: list[dict[str, Any]], error: str) -> None:
    with pytest.raises(ProtocolError, match="^" + re.escape(error)):
        _decision(DONE, OPEN, gates=changes)


def test_a_closed_gate_cannot_be_changed() -> None:
    closed = PlanView(started={"s1": (), "g1": ()}, pending={}, done=frozenset({"g1"}),
                      gates={"g1": GateView(GateState.PASSED, AGENDA)})
    with pytest.raises(ProtocolError, match="is not a gate that is still open or not opened yet \\(those: none\\)"):
        parse_chat(_reply({"reply": "ok", "action": "none",
                           "gates": [{"id": "g1", "action": "cancel", "resolution": "r"}]}), closed)


def test_cancelling_the_other_open_gates_makes_the_decision_complete_the_task() -> None:
    plan = PlanView(started={"s1": (), "g1": ()}, pending={}, running=frozenset(),
                    gates={"g1": GateView(GateState.OPEN, AGENDA)})
    context = DecisionContext(step="s1", result=ResultKind.WORK, succeeded=True, review_required=False, retries=0,
                              open_gates=frozenset({"g1"}))
    cancel = [{"id": "g1", "action": "cancel", "resolution": "s1 answered it"}]
    with pytest.raises(ProtocolError, match='this decision completes the task, so it must carry "final"'):
        _decision(context, plan, gates=cancel)
    assert _decision(context, plan, gates=cancel, final=FINAL).final is not None
    # Without the cancel the gate stays open, so the accept completes nothing.
    assert _decision(context, plan).final is None
    assert _decision(context, plan).decision is Decision.ACCEPT


def test_a_chat_reply_may_cancel_a_gate_and_depend_on_it_in_a_replan() -> None:
    reply = parse_chat(_reply({"reply": "s1 answered it", "action": "replan", "steps": [_step("s3", "g1")],
                               "gates": [{"id": "g1", "action": "cancel", "resolution": "answered by s1"}]}), OPEN)
    assert reply.gates[0].action is GateAction.CANCEL and [s.id for s in reply.steps] == ["s3"]


# What the contract was before gates, for each combination (sha256): with gates off it is byte for byte the same.
_BEFORE = {(False, 2): "eaf7d704210243ea908d0dac3b50ab65a5597f1bb35e609ef6eff5ebd4e3bed8",
           (False, 0): "e8092df608ce4226b94bb019020297fccaed774a35b5b331c4adb0616669e77b",
           (True, 2): "ac7a9b12adb623a4bcce0d64c42e0d78f9add0a238a8b1bab585f4092852cd99",
           (True, 5): "d989a3235b9cabcb7b8d4c2d6a8813ebdb1ac8147e7820c8041afc3cc48ce795"}


@pytest.mark.parametrize(("roles", "retries"), sorted(_BEFORE))
def test_the_contract_without_gates_is_the_contract_before_them(roles: bool, retries: int) -> None:
    text = reply_contract(retries, roles=roles)
    assert hashlib.sha256(text.encode()).hexdigest() == _BEFORE[(roles, retries)]
    assert "[GATE]" not in text and '"kind": "gate"' not in text


def test_the_contract_with_gates_says_how_to_open_discuss_change_and_close_them() -> None:
    text = reply_contract(2, gates=True)
    assert "starts with [PLAN], [DECISION], [CHAT], [RESTART] or\n[GATE]." in text
    assert '{"id": "g1", "kind": "gate", "topic": "...", "agenda": [{"question": "...", "options"' in text
    assert "Open one only when all three hold" in text
    assert '- gates (optional, also in [CHAT]): change gates not closed yet' in text
    assert "[GATE]: the user wrote in a gate's discussion, or ended it." in text
    assert "What the user writes in a gate's\n  discussion comes as [GATE], not as a supplement." in text
    assert text.index("[RESTART]:") < text.index("[GATE]: the user") < text.index("[SUPERVISE]:")


def test_a_task_a_schedule_started_never_opens_a_gate_whatever_its_project_says() -> None:
    assert gate_policy(gates_on=True, scheduled=True) is GatePolicy.UNATTENDED
    assert gate_policy(gates_on=False, scheduled=False) is GatePolicy.OFF
    assert gate_policy(gates_on=True, scheduled=False) is GatePolicy.ALLOWED


def test_new_agenda_items_are_numbered_after_the_highest_id() -> None:
    items = numbered([AgendaItem("q2", "b"), AgendaItem("", "c"), AgendaItem("", "d")],
                     (AgendaItem("q1", "a"), AgendaItem("q2", "b")))
    assert [i.id for i in items] == ["q2", "q3", "q4"]


def test_the_conclusion_says_what_was_settled_assumed_and_left_open() -> None:
    text = conclusion_text("g1", "storage", GateSummary(("PostgreSQL",), ("90 days is enough",), ("backups",)))
    assert text == ("Settled with the user at gate g1 (storage):\nDecisions:\n- PostgreSQL\n"
                    "Assumptions (not confirmed by the user):\n- 90 days is enough\nStill open:\n- backups")
