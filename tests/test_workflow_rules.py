"""Workflow rules at the Planner's admission and result entry points."""
from __future__ import annotations

import json
from unittest.mock import AsyncMock

import pytest

from looper.agents.task_roles import RoleKind
from looper.agents.tiers.planner_protocol import (
    Decision, DecisionContext, PlanView, ProtocolError, ResultKind, parse_decision, parse_plan,
)
from looper.agents.user_gates import GatePolicy
from tests.test_planner_agent import (
    REVIEW_PASS, WORK_FAILED, WORK_OK, _project, always, decide, drive, plan, step,
)

VERIFY = "verify_staging"
ROLES = {"dev": RoleKind.WORK, VERIFY: RoleKind.WORK}
GATE = {"id": "g1", "kind": "gate", "topic": "Choose next action", "depends_on": [],
        "agenda": [{"question": "Continue verification?"}]}


def workflow(on=True, **overrides):
    return {"key": "checked", "name": "Checked work", "review_all": False,
            "roles": [{"key": key, "kind": "work"} for key in ROLES],
            **({"verification": {"role": VERIFY, "max_rounds": 2}} if on else {}), **overrides}


def configure(agent_db, monkeypatch, on=True, **overrides):
    from looper.db import task_roles

    agent_db.get_task_project.return_value = {**_project(), "max_step_retries": 4,
                                              "task_workflow": workflow(on, **overrides)}
    rows = [{"task_id": "t1", "role_key": key, "version": 1, "kind": "work", "via": "workflow",
             "role_prompt": "Run the assigned work.", "harness": None, "profile_id": None,
             "reason": "", "profile_name": None, "profile_allowed": False} for key in ROLES]
    monkeypatch.setattr(task_roles, "current", AsyncMock(return_value=rows))
    monkeypatch.setattr(task_roles, "get", AsyncMock(side_effect=lambda task, key, *a, **kw:
                                                    next(r for r in rows if r["role_key"] == key)))


@pytest.mark.parametrize("on", [True, False])
def test_verification_requires_a_work_dependency(on):
    candidate = plan(GATE, {**step("v1", "g1"), "role": VERIFY})
    options = {"verification_role": VERIFY} if on else {}
    if on:
        with pytest.raises(ProtocolError, match="work step"):
            parse_plan(candidate, ROLES, gate_policy=GatePolicy.ALLOWED, **options)
    else:
        assert parse_plan(candidate, ROLES, gate_policy=GatePolicy.ALLOWED).steps


@pytest.mark.parametrize("explicit", [False, True])
@pytest.mark.parametrize("on", [True, False])
def test_verification_review_exemption_and_dispatch(planner_claude, agent_db, monkeypatch, on, explicit):
    configure(agent_db, monkeypatch, on, review_all=True)
    spec = {**step("v1", "s1", review=explicit), "role": VERIFY}
    needs_review = explicit or not on
    replies = [plan(step("s1"), spec), decide("review"), decide("accept")]
    replies += ([decide("review")] if needs_review else []) + [decide("accept")]
    h = drive(planner_claude(*replies), always(WORK_OK, REVIEW_PASS))
    assert h.final().payload["status"] == "completed"
    assert len(h.reviews) == 1 + needs_review
    assert h.work_items[1].payload.get("verification", False) is on


@pytest.mark.parametrize("on", [True, False])
def test_second_verification_failure_caps_retries_across_step_ids(planner_claude, agent_db, monkeypatch, on):
    configure(agent_db, monkeypatch, on)
    first = {**step("v1", "s1"), "role": VERIFY}
    second = {**step("v2", "s1"), "role": VERIFY}
    claude = planner_claude(plan(step("s1"), first), decide("accept"),
                            decide("replan", steps=[second], reason="Check again"),
                            decide("retry", feedback="Repeat verification"), decide("fail", reason="Stop"))
    h = drive(claude, lambda e: WORK_FAILED if e.payload.get("step_key", "").startswith("v") else WORK_OK)
    assert h.final().payload["status"] == "failed"
    assert len(h.work_items) == (3 if on else 4)
    rounds = [json.loads(e["summary"]) for e in agent_db.store.journal["t1"]
              if e["kind"] == "verification_round"]
    assert len(rounds) == (2 if on else 0)
    if on:
        assert all(r["status"] == "failed" and "head" in r for r in rounds)
        assert "retry is not allowed" in claude.turns()[-1]["text"]


@pytest.mark.parametrize("on", [True, False])
def test_exhausted_verification_admission_requires_a_new_gate(on):
    view = PlanView(started={"s1": ()}, pending={}, roles=ROLES, gate_policy=GatePolicy.ALLOWED,
                    verification_role=VERIFY if on else None, verification_exhausted=on)
    context = DecisionContext("v1", ResultKind.WORK, False, False, 0)
    candidate = decide("replan", steps=[{**step("v2", "s1"), "role": VERIFY}])
    if on:
        with pytest.raises(ProtocolError, match="new gate"):
            parse_decision(candidate, context, view)
    else:
        assert parse_decision(candidate, context, view).steps
    gated = decide("replan", steps=[GATE, {**step("v2", "s1", "g1"), "role": VERIFY}])
    assert len(parse_decision(gated, context, view).steps) == 2


@pytest.mark.parametrize("on", [True, False])
def test_exhaustion_escalation_rejects_review_and_ungated_replan(on):
    context = DecisionContext("s1", ResultKind.WORK, True, True, 0, max_retries=0, escalate=on)
    assert context.allowed() == ((Decision.REPLAN, Decision.FAIL) if on else
                                 (Decision.REVIEW, Decision.REPLAN, Decision.FAIL))
    view = PlanView(started={"s1": ()}, pending={}, gate_policy=GatePolicy.ALLOWED)
    candidate = decide("replan", steps=[step("s2")])
    if on:
        with pytest.raises(ProtocolError, match="new gate"):
            parse_decision(candidate, context, view)
    else:
        assert parse_decision(candidate, context, view).steps
    assert parse_decision(decide("replan", steps=[GATE, step("s2", "g1")]), context, view).steps


@pytest.mark.parametrize("on", [True, False])
def test_escalation_runs_on_result_path_and_records_gate(planner_claude, agent_db, monkeypatch, on):
    configure(agent_db, monkeypatch, False, max_step_retries=0, escalate_on_exhaustion=on)
    candidate = decide("replan", steps=[step("s2")], reason="Try replacement")
    gated = decide("replan", steps=[GATE, step("s3", "g1")], reason="Ask before continuing")
    claude = planner_claude(plan(step("s1")), candidate, gated if on else decide("fail", reason="Stop"))

    async def scenario(h):
        await h.start()
        if on:
            await h.until(lambda: h.planner._plan and h.planner._plan.waiting_gates())
        else:
            await h.finished()

    h = drive(claude, always(WORK_FAILED), scenario)
    entries = [e for e in agent_db.store.journal["t1"] if e["kind"] == "escalation"]
    assert len(entries) == int(on)
    assert len(h.work_items) == (1 if on else 2)
    assert ("Escalation required" in claude.turns()[1]["text"]) is on


def test_default_planner_prompt_keeps_its_bytes_without_workflow():
    from looper.agents.agent_settings import DEFAULT_HARNESS_PROMPT, Harness, Layer
    from looper.agents.system_prompt import build_system_prompt, plan_task_section
    from looper.agents.tiers.planner import DEFAULT_SYSTEM_PROMPT
    from looper.agents.tiers.planner_protocol import reply_contract

    prompt = build_system_prompt(
        harness=Harness(DEFAULT_HARNESS_PROMPT, Layer.BUILTIN), role_id="planner",
        role=DEFAULT_SYSTEM_PROMPT, recommended=True, task_kind="plan", task_id="task-baseline",
        task=plan_task_section(task_id="task-baseline", requirement="Build the project",
                               project_name="Example", work_dir=""),
        contract_name="REPLY_CONTRACT", contract=reply_contract())
    assert prompt.sha256 == "c18430ad52efc890b7e61cefba368cb90e4b9747e1a346f65057b0d45d636e5c"


@pytest.mark.parametrize("rule", ["verification", "escalation"])
def test_workflow_prerequisites_cannot_be_bypassed_by_any_of(rule):
    view = PlanView(started={"s1": ()}, pending={}, roles=ROLES, gate_policy=GatePolicy.ALLOWED,
                    conditions=True, verification_role=VERIFY, verification_exhausted=rule == "verification")
    context = DecisionContext("old", ResultKind.WORK, False, False, 0, escalate=rule == "escalation")
    spec = {**step("v2", "s1", "g1"), "role": VERIFY,
            "ready_when": {"any_of": ["s1", "g1"]}}
    with pytest.raises(ProtocolError, match="work step|new gate"):
        parse_decision(decide("replan", steps=[GATE, spec]), context, view)
    spec["ready_when"] = {"all_of": ["s1", "g1"]}
    assert parse_decision(decide("replan", steps=[GATE, spec]), context, view).steps


@pytest.mark.parametrize("on", [True, False])
def test_failed_escalation_decision_cannot_accept_an_unreviewed_result(
    planner_claude, agent_db, monkeypatch, on,
):
    configure(agent_db, monkeypatch, False, max_step_retries=0, escalate_on_exhaustion=on)
    claude = planner_claude(plan(step("s1", review=True)), "<error>")
    h = drive(claude, always(WORK_OK))
    assert h.final().payload["status"] == ("failed" if on else "completed")
    assert not h.reviews


@pytest.mark.parametrize("on", [True, False])
def test_stopped_verification_attempts_also_exhaust_rounds(planner_claude, agent_db, monkeypatch, on):
    configure(agent_db, monkeypatch, on)
    claude = planner_claude(plan(step("s1"), {**step("v1", "s1"), "role": VERIFY}), decide("accept"),
                            decide("retry", feedback="Check again"), decide("retry", feedback="Check again"),
                            decide("fail", reason="Stop"))

    async def scenario(h):
        await h.start()
        await h.until(lambda: len(h.held) == 1)
        for _ in range(2):
            await h.planner._kill_and_decide(h.planner._plan, h.planner._plan.steps[1], "Check stalled")
        if not on:
            await h.release(WORK_FAILED)
        await h.finished()

    h = drive(claude, lambda e: None if e.payload.get("step_key") == "v1" else WORK_OK, scenario)
    assert h.final().payload["status"] == "failed"
    assert len(h.work_items) == (3 if on else 4)
    assert sum(e["kind"] == "verification_round" for e in agent_db.store.journal["t1"]) == (2 if on else 0)


@pytest.mark.parametrize("on", [True, False])
def test_verification_start_failures_also_exhaust_rounds(planner_claude, agent_db, monkeypatch, on):
    from looper.db import task_roles

    configure(agent_db, monkeypatch, on)
    monkeypatch.setattr(task_roles, "get", AsyncMock(return_value=None))
    claude = planner_claude(plan(step("s1"), {**step("v1", "s1"), "role": VERIFY}), decide("accept"),
                            decide("retry", feedback="Retry launch"), decide("retry", feedback="Retry launch"),
                            decide("fail", reason="Role unavailable"))
    h = drive(claude, always(WORK_OK))
    assert h.final().payload["status"] == "failed"
    rounds = [e for e in agent_db.store.journal["t1"] if e["kind"] == "verification_round"]
    assert len(rounds) == (2 if on else 0)
    assert len(h.work_items) == 1
    assert h.planner._plan.steps[1].retries == (1 if on else 2)
