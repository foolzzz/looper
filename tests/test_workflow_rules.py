"""Workflow rules at the Planner's admission and result entry points."""
from __future__ import annotations

import json
from unittest.mock import AsyncMock

import pytest

from looper.agents.task_roles import RoleKind
from looper.agents.message import MessageEnvelope, MessageType, TASK_ROUTER_ADDRESS
from looper.agents.tiers.planner import PlannerAgent
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
    assert prompt.sha256 == "25b30ea66f08b6e7b7cba1c492e58b058ba2cd64bd154b5121b5ca45bf604626"


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
def test_failed_decision_at_retry_limit_still_requires_review_with_escalation(
    planner_claude, agent_db, monkeypatch, on,
):
    configure(agent_db, monkeypatch, False, max_step_retries=0, escalate_on_exhaustion=on)
    claude = planner_claude(plan(step("s1", review=True)), "<error>")
    h = drive(claude, always(WORK_OK, REVIEW_PASS))
    assert h.final().payload["status"] == "completed"
    assert len(h.reviews) == int(on)


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


@pytest.mark.parametrize("retries", [0, 1])
def test_success_at_retry_limit_still_enters_first_review(planner_claude, agent_db, monkeypatch, retries):
    configure(agent_db, monkeypatch, False, review_all=True, max_step_retries=retries,
              escalate_on_exhaustion=True)
    replies = [plan(step("s1"))]
    if retries:
        replies.append(decide("retry", feedback="Fix the failure"))
    replies += [decide("review"), decide("accept")]
    h = drive(planner_claude(*replies), lambda e: REVIEW_PASS if e.message_type is MessageType.VERIFY_REQUEST else
              (WORK_FAILED if retries and e.payload["attempt"] == 1 else WORK_OK))
    assert h.final().payload["status"] == "completed"
    assert len(h.work_items) == retries + 1
    assert len(h.reviews) == 1


def test_unavailable_verification_snapshot_allows_retry_without_repair(planner_claude, agent_db, monkeypatch):
    configure(agent_db, monkeypatch)
    verify = {**step("v1", "s1"), "role": VERIFY}
    claude = planner_claude(plan(step("s1"), verify), decide("accept"),
                            decide("retry", feedback="Retry repository snapshot"), decide("accept"))
    h = drive(claude, lambda e: {**WORK_FAILED, "failure": "verification_unavailable"}
              if e.payload.get("verification") and e.payload["attempt"] == 1 else WORK_OK)
    assert h.final().payload["status"] == "completed"
    assert len(h.work_items) == 3
    rounds = [json.loads(e["summary"]) for e in agent_db.store.journal["t1"]
              if e["kind"] == "verification_round"]
    assert len(rounds) == 2
    assert all("repair_after_index" not in r for r in rounds)


@pytest.mark.parametrize("bypass", ["retry", "replace", "unreviewed", "verification_repair", "condition"])
def test_verification_code_change_requires_reviewed_repair(planner_claude, agent_db, monkeypatch, bypass):
    configure(agent_db, monkeypatch)
    repair = step("fix", "s1", review=True)
    verification = {**step("v2", "fix"), "role": VERIFY}
    candidates = {
        "retry": decide("retry", feedback="Try the dirty baseline"),
        "replace": decide("replan", steps=[{**step("v2", "s1"), "role": VERIFY}]),
        "unreviewed": decide("replan", steps=[step("fix", "s1"), verification]),
        "verification_repair": decide("replan", steps=[{**repair, "role": VERIFY}, verification]),
        "condition": decide("replan", steps=[repair, {**verification, "depends_on": ["s1", "fix"],
                                      "ready_when": {"any_of": ["s1", "fix"]}}]),
    }
    if bypass == "condition":
        agent_db.get_task_project.return_value["task_workflow"]["dependency_conditions"] = True
    claude = planner_claude(plan(step("s1"), {**step("v1", "s1"), "role": VERIFY}), decide("accept"),
                            candidates[bypass], decide("replan", steps=[repair, verification]),
                            decide("review"), decide("accept"), decide("accept"))

    def outcome(e):
        if e.message_type is MessageType.VERIFY_REQUEST:
            return REVIEW_PASS
        if e.payload["step_key"] == "v1":
            return {**WORK_FAILED, "failure": "verification_changed_code"}
        return WORK_OK

    h = drive(claude, outcome)
    assert h.final().payload["status"] == "completed"
    assert [e.payload["step_key"] for e in h.work_items] == ["s1", "v1", "fix", "v2"]
    assert [e.payload["step_key"] for e in h.reviews] == ["fix"]
    rounds = [json.loads(e["summary"]) for e in agent_db.store.journal["t1"]
              if e["kind"] == "verification_round"]
    assert rounds[0]["failure"] == "verification_changed_code"


def test_verification_repair_requirement_survives_replan_and_restart(planner_claude, agent_db, monkeypatch):
    configure(agent_db, monkeypatch)
    repair = step("fix", "s1", review=True)
    verification = {**step("v2", "fix"), "role": VERIFY}
    claude = planner_claude(plan(step("s1"), {**step("v1", "s1"), "role": VERIFY}), decide("accept"),
                            decide("replan", steps=[repair, verification]),
                            decide("review"), decide("accept"), decide("accept"))
    resumed = False

    def outcome(e):
        if e.message_type is MessageType.VERIFY_REQUEST:
            return REVIEW_PASS
        if e.payload["step_key"] == "v1":
            return {**WORK_FAILED, "failure": "verification_changed_code"}
        if e.payload["step_key"] == "fix" and not resumed:
            return None
        return WORK_OK

    async def scenario(h):
        nonlocal resumed
        await h.start()
        await h.until(lambda: len(h.held) == 1)
        await h.planner.stop()
        saved = {s["key"]: s for s in agent_db.store.steps["t1"].values()}
        assert saved["v1"]["status"] == "skipped"
        agent_db.store.tasks["t1"] = {"requirement": "build the calc package", "work_dir": str(h.work_dir)}
        h.held.clear()
        resumed = True
        h.planner = PlannerAgent(task_id="t1", runtime_root=h.work_dir.parent, claude_bin=claude.bin)
        # Pause the resumed repair so admission is checked against the reloaded, unresolved violation.
        h.outcome = lambda e: None if e.payload["step_key"] == "fix" else outcome(e)
        await h.planner.start(h.bus)
        await h.bus.publish(MessageEnvelope.create(source=TASK_ROUTER_ADDRESS, target=h.planner.agent_address,
            message_type=MessageType.TASK_RESUME, payload={"task_id": "t1"}, correlation_id="corr-1"))
        await h.until(lambda: len(h.held) == 1)
        await h.planner._read_roles()
        checked = h.planner._checked(h.planner._plan.view())
        assert checked.verification_repair_required is True
        with pytest.raises(ProtocolError, match="repair step"):
            parse_decision(decide("replan", steps=[{**step("v3", "s1"), "role": VERIFY}]),
                           DecisionContext("fix", ResultKind.WORK, False, True, 0), checked)
        h.outcome = outcome
        await h.release(WORK_OK)
        await h.finished()
        assert h.planner._checked(h.planner._plan.view()).verification_repair_required is False

    h = drive(claude, outcome, scenario)
    assert h.final().payload["status"] == "completed"
    assert [e.payload["step_key"] for e in h.reviews] == ["fix"]
    assert [e.payload["step_key"] for e in h.work_items].count("v1") == 1


def test_skipping_repair_cannot_complete_task(planner_claude, agent_db, monkeypatch):
    configure(agent_db, monkeypatch)
    claude = planner_claude(plan(step("s1"), {**step("v1", "s1"), "role": VERIFY}), decide("accept"),
                            decide("replan", steps=[step("fix", "s1", review=True)]))

    async def scenario(h):
        await h.start()
        await h.until(lambda: len(h.held) == 1)
        # A conditional branch or a user cancellation may skip the repair after admission.
        h.planner._skip(h.planner._plan.steps[-1])
        await h.planner._complete_plan(h.planner._plan)
        await h.finished()

    h = drive(claude, lambda e: None if e.payload["step_key"] == "fix" else
              ({**WORK_FAILED, "failure": "verification_changed_code"} if e.payload["step_key"] == "v1"
               else WORK_OK), scenario)
    assert h.final().payload["status"] == "failed"
    assert not h.reviews


def test_failed_review_at_retry_limit_still_escalates(planner_claude, agent_db, monkeypatch):
    from tests.test_planner_agent import REVIEW_FAIL

    configure(agent_db, monkeypatch, False, review_all=True, max_step_retries=0, escalate_on_exhaustion=True)
    claude = planner_claude(plan(step("s1")), decide("review"), decide("review"), decide("fail", reason="Stop"))
    h = drive(claude, always(WORK_OK, REVIEW_FAIL))
    assert h.final().payload["status"] == "failed"
    assert len(h.reviews) == 1
    assert "review is not allowed" in claude.turns()[-1]["text"]


@pytest.mark.parametrize("rule", ["verification", "escalation"])
@pytest.mark.parametrize("pending", [True, False])
def test_exhaustion_gate_cannot_be_cancelled_by_parallel_result(
    planner_claude, agent_db, monkeypatch, rule, pending,
):
    from looper.agents.user_gates import Gate
    from tests.test_planner_gates import _write, gate_reply, SETTLED, SUMMARY

    verification = rule == "verification"
    configure(agent_db, monkeypatch, verification, max_step_retries=0, escalate_on_exhaustion=not verification,
              **({"verification": {"role": VERIFY, "max_rounds": 1}} if verification else {}))
    failed = {**step("failed", "s1"), **({"role": VERIFY} if verification else {})}
    following = {**step("following", "s1", "g1"), **({"role": VERIFY} if verification else {})}
    gate = {**GATE, "depends_on": ["check"] if pending else []}
    cancel = [{"id": "g1", "action": "cancel", "resolution": "Parallel check passed"}]
    claude = planner_claude(
        plan(step("s1"), step("check"), failed), decide("accept"),
        decide("replan", steps=[gate, following], reason="Ask before more work"),
        decide("accept", gates=cancel), decide("accept"),
        gate_reply("Continue", "close", SETTLED, summary=SUMMARY), decide("accept"),
    )

    def outcome(envelope):
        key = envelope.payload["step_key"]
        return None if key == "check" else WORK_FAILED if key == "failed" else WORK_OK

    async def scenario(h):
        await h.start()
        await h.until(lambda: bool(agent_db.store.gates.get("t1")))
        saved = next(iter(agent_db.store.gates["t1"].values()))
        assert saved["state"] == ("pending" if pending else "open")
        await h.release(WORK_OK)
        await h.until(lambda: len(claude.turns()) >= 5 and (h.final() or h.planner._plan.waiting_gates()))
        assert [e.payload["step_key"] for e in h.work_items] == ["s1", "check", "failed"]
        assert "cannot cancel" in claude.turns()[4]["text"]
        # Readback must retain the protection even if the Planner is restarted.
        restored = Gate.from_row(next(iter(agent_db.store.gates["t1"].values())))
        assert restored.row(saved["step_id"])["purpose"] == (
            "verification_exhaustion" if verification else "exhaustion")
        await _write(h, agent_db, "Continue after the exhausted attempts")
        await h.finished()

    h = drive(claude, outcome, scenario)
    assert h.final().payload["status"] == "completed"
    assert [e.payload["step_key"] for e in h.work_items] == ["s1", "check", "failed", "following"]
    assert next(iter(agent_db.store.gates["t1"].values()))["state"] == "passed"


@pytest.mark.parametrize("condition", [None, {"all_of": ["s1", "g1"]}])
def test_exhaustion_gate_dependency_requires_passed_not_cancelled(condition, tmp_path):
    from looper.agents.tiers.conditions import State

    view = PlanView(started={"s1": ()}, pending={}, roles=ROLES, gate_policy=GatePolicy.ALLOWED,
                    conditions=True, verification_role=VERIFY, verification_exhausted=True)
    candidate = {**step("v2", "s1", "g1"), "role": VERIFY}
    if condition is not None:
        candidate["ready_when"] = condition
    parsed = parse_decision(decide("replan", steps=[GATE, candidate]),
                            DecisionContext("old", ResultKind.WORK, False, False, 0), view)
    agent = PlannerAgent(task_id="t1", runtime_root=tmp_path)
    from looper.agents.tiers.planner import PlanStep, TaskPlan
    from looper.agents.user_gates import GateState

    task = TaskPlan(goal_id="goal", plan_id="plan", requirement="verify", work_dir=str(tmp_path),
                    steps=[PlanStep("s1", 0, "work", key="s1", status="verified")])
    gate, verify = agent._add_steps(task, parsed.steps)
    gate.status, gate.gate.state = "verified", GateState.CANCELLED
    assert verify not in task.ready_steps()
    assert task.ready_state(verify) is State.UNSATISFIABLE
    gate.gate.state = GateState.PASSED
    assert verify in task.ready_steps()


@pytest.mark.parametrize("purpose", ["exhaustion", "verification_exhaustion"])
@pytest.mark.parametrize("turn", ["decision", "chat"])
@pytest.mark.parametrize("state", ["pending", "open"])
def test_restored_exhaustion_gate_refuses_planner_cancellation(turn, state, purpose):
    from looper.agents.tiers.planner_protocol import parse_chat
    from looper.agents.user_gates import Gate, GateView
    from tests.test_planner_agent import chat

    gate = Gate.from_row({"topic": "Continue?", "state": state, "purpose": purpose})
    view = PlanView(started={"s1": ()}, pending={"g1": ()},
                    gates={"g1": GateView(gate.state, gate.agenda, gate.purpose)})
    cancel = [{"id": "g1", "action": "cancel", "resolution": "The check passed"}]
    with pytest.raises(ProtocolError, match="cannot cancel exhaustion gate"):
        if turn == "chat":
            parse_chat(chat("Continue", gates=cancel), view)
        else:
            parse_decision(decide("accept", gates=cancel),
                           DecisionContext("s1", ResultKind.WORK, True, False, 0), view)


def test_replan_cannot_drop_or_downgrade_a_pending_exhaustion_gate():
    from looper.agents.tiers.planner_protocol import parse_chat
    from looper.agents.user_gates import GatePurpose, GateState, GateView
    from tests.test_planner_agent import chat

    view = PlanView(started={"s1": ()}, pending={"g1": ()}, gate_policy=GatePolicy.ALLOWED,
                    gates={"g1": GateView(GateState.PENDING, purpose=GatePurpose.EXHAUSTION)})
    with pytest.raises(ProtocolError, match="keep the exhaustion gate"):
        parse_chat(chat("Replace the plan", "replan", steps=[step("s2")]), view)
    parsed = parse_chat(chat("Keep the decision", "replan", steps=[GATE, step("s2", "g1")]), view)
    assert parsed.steps[0].gate.purpose is GatePurpose.EXHAUSTION
    from looper.agents.tiers.conditions import to_json

    assert to_json(parsed.steps[1].ready_when, lambda label: label) == {
        "all_of": [{"step": "g1", "on": "passed"}]}


@pytest.mark.parametrize("action", ["add_steps", "replan"])
def test_later_chat_cannot_add_ungated_work_while_escalation_waits(action):
    from looper.agents.tiers.planner_protocol import parse_chat
    from looper.agents.user_gates import GatePurpose, GateState, GateView
    from tests.test_planner_agent import chat

    view = PlanView(started={"s1": ()}, pending={"g1": (), "following": ("g1",)},
                    gate_policy=GatePolicy.ALLOWED,
                    gates={"g1": GateView(GateState.PENDING, purpose=GatePurpose.EXHAUSTION)})
    steps = [GATE, step("following")] if action == "replan" else [step("replacement")]
    with pytest.raises(ProtocolError, match="exhaustion gate"):
        parse_chat(chat("Continue without authorization", action, steps=steps), view)
