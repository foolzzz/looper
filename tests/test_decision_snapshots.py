"""Decision snapshots replay what code decided from what it read, and keep no prompt or message text."""

from __future__ import annotations

import json
from typing import Any

import pytest

from looper.agents.step_workspaces import Repo, Unusable, plan_round
from looper.agents.tiers.decisions import Dispatch, _context, _context_of, replay, turn_digest
from looper.agents.tiers.planner import PlanStep, TaskPlan
from looper.agents.tiers.planner_protocol import Decision, DecisionContext, ResultKind
from looper.agents.user_gates import Gate, GateState
from looper.agents.waits import Wait, WaitOutcome, WaitState


def _step(key: str, status: str = "pending", *depends_on: str, **fields: Any) -> PlanStep:
    return PlanStep(step_id=f"id-{key}", index=0, instruction=f"do {key}", key=key, status=status,
                    depends_on=[f"id-{d}" for d in depends_on], **fields)


def _gate(state: GateState) -> Gate:
    return Gate("Choose", (), state=state)


def _row(inputs: dict[str, Any], decision: dict[str, Any], kind: str = "dispatch") -> dict[str, Any]:
    """The snapshot as the database hands it back (through JSON)."""
    return {"id": 1, "kind": kind, "inputs": json.loads(json.dumps(inputs)),
            "decision": json.loads(json.dumps(decision))}


def _dispatched(plan: TaskPlan, isolate_steps: bool = False, round_: tuple[list[bool], Repo | Unusable] | None = None,
                ) -> dict[str, Any]:
    """What the Planner's dispatch pass records for ``plan``: ready steps, then the round over its work steps."""
    ready = plan.ready_steps(isolate_steps=isolate_steps)
    record = Dispatch(plan, isolate_steps=isolate_steps)
    start = [s for s in ready if s.kind == "work"]
    if round_ is not None:
        in_flight, repo = round_
        flags = [False] * len(start)
        decided = plan_round(in_flight=in_flight, ready=flags, repo=repo)
        record.round(in_flight, flags, repo, decided)
        start = start[:decided.start]
    return _row(record.inputs, record.decision(ready, start))


CASES = {
    "chain": (TaskPlan("p", "g", "r", "/w", steps=[
        _step("s1", "verified"), _step("s2", "pending", "s1"), _step("s3", "pending", "s2")]), False, None,
        ["s2"], ["s2"]),
    "gate, wait and work together": (TaskPlan("p", "g", "r", "/w", steps=[
        _step("s1", "verified"), _step("g1", "pending", "s1", gate=_gate(GateState.PENDING)),
        _step("w1", "pending", "s1", wait=Wait(None, WaitState.PENDING)), _step("s2", "pending", "s1"),
        _step("s3", "pending", "g1")]), False, None, ["g1", "w1", "s2"], ["s2"]),
    "a passed gate and a cancelled one": (TaskPlan("p", "g", "r", "/w", steps=[
        _step("g1", "verified", gate=_gate(GateState.PASSED)), _step("g2", "verified", gate=_gate(GateState.CANCELLED)),
        _step("s1", "pending", ready_when={"step": "id-g1", "on": "passed"}),
        _step("s2", "pending", ready_when={"step": "id-g2", "on": "passed"})]), False, None, ["s1"], ["s1"]),
    "a finished wait": (TaskPlan("p", "g", "r", "/w", steps=[
        _step("w1", "verified", wait=Wait(None, WaitState.DONE, WaitOutcome.READY)),
        _step("s1", "pending", ready_when={"step": "id-w1", "on": "passed"})]), False, None, ["s1"], ["s1"]),
    "any of a failed and a verified source": (TaskPlan("p", "g", "r", "/w", steps=[
        _step("s1", "failed", result={"status": "failed"}), _step("s2", "verified"),
        _step("s3", "pending", ready_when={"any_of": ["id-s1", "id-s2"]})]), False, None, ["s3"], ["s3"]),
    # With step workspaces a completed leaf waits for acceptance, except for a review seat.
    "completed leaves with step workspaces": (TaskPlan("p", "g", "r", "/w", steps=[
        _step("s1", "completed", result={"status": "completed"}),
        _step("seat", "pending", reviews="id-s1", ready_when={"step": "id-s1", "on": "completed"}),
        _step("s2", "pending", ready_when={"step": "id-s1", "on": "completed"})]), True, None, ["seat"], []),
    "completed leaves without them": (TaskPlan("p", "g", "r", "/w", steps=[
        _step("s1", "completed", result={"status": "completed"}),
        _step("seat", "pending", reviews="id-s1", ready_when={"step": "id-s1", "on": "completed"}),
        _step("s2", "pending", ready_when={"step": "id-s1", "on": "completed"})]), False, None, ["seat", "s2"],
        ["s2"]),
    "a review seat by its verdict": (TaskPlan("p", "g", "r", "/w", steps=[
        _step("s1", "verified"), _step("seat", "verified", reviews="id-s1", result={"passed": False}),
        _step("s2", "pending", ready_when={"step": "id-seat", "on": "passed"}),
        _step("s3", "pending", ready_when={"step": "id-seat", "on": "verified"})]), False, None, ["s3"], ["s3"]),
    "a round that waits for the task directory": (TaskPlan("p", "g", "r", "/w", steps=[
        _step("s1", "dispatched"), _step("s2"), _step("s3")]), True, ([False], Repo("/r", "", "b", "h")),
        ["s2", "s3"], []),
    "a round in worktrees": (TaskPlan("p", "g", "r", "/w", steps=[
        _step("s1", "dispatched"), _step("s2"), _step("s3")]), True, ([True], Repo("/r", "", "b", "h")),
        ["s2", "s3"], ["s2", "s3"]),
    "a round one at a time": (TaskPlan("p", "g", "r", "/w", steps=[_step("s2"), _step("s3")]), True,
        ([], Unusable("detached HEAD", repository=True)), ["s2", "s3"], ["s2"]),
}


@pytest.mark.parametrize(("plan", "isolate", "round_", "ready", "start"), CASES.values(), ids=CASES.keys())
def test_a_dispatch_replays_to_what_it_decided(plan, isolate, round_, ready, start):
    row = _dispatched(plan, isolate, round_)
    assert [item["step"].removeprefix("id-") for item in row["decision"]["ready"]] == ready
    assert [step.removeprefix("id-") for step in row["decision"]["start"]] == start
    result = replay(row)
    assert result.same, (result.recorded, result.replayed, result.note)


def test_a_dispatch_keeps_only_what_readiness_reads():
    plan = TaskPlan("p", "g", "r", "/w", steps=[_step("s1", "completed", result={
        "status": "completed", "summary": "the Worker's report", "changed_files": {"a.py": "added"}})])
    inputs = Dispatch(plan, isolate_steps=False).inputs
    assert inputs["steps"] == [{"id": "id-s1", "key": "s1", "kind": "work", "status": "completed", "depends_on": [],
                                "ready_when": None, "reviews": None, "result": {"status": "completed"},
                                "gate": None, "wait": None}]
    assert "do s1" not in json.dumps(inputs) and "report" not in json.dumps(inputs)


@pytest.mark.parametrize("change", ["start", "ready", "round", "version", "malformed"])
def test_a_dispatch_that_does_not_replay_says_so(change):
    row = _dispatched(TaskPlan("p", "g", "r", "/w", steps=[_step("s1", "dispatched"), _step("s2"), _step("s3")]),
                      True, ([False], Repo("/r", "", "b", "h")))
    if change == "start":
        row["decision"]["start"] = ["id-s2"]
    elif change == "ready":
        row["decision"]["ready"] = row["decision"]["ready"][:1]
    elif change == "round":
        row["inputs"]["round"]["in_flight"] = [True]  # the replay starts both, which was not recorded
    elif change == "version":
        row["inputs"]["version"] = 99
    else:
        del row["inputs"]["steps"][0]["status"]
    result = replay(row)
    assert not result.same
    assert ("format 99" in result.note) if change == "version" else ("KeyError" in result.note) \
        if change == "malformed" else result.replayed is not None


def _decision_row(context: DecisionContext, allowed: list[str], decision: str) -> dict[str, Any]:
    return _row({"version": 1, "step": "id-s1", "key": "s1", "attempt": 1, "context": _context(context),
                 "turn": None}, {"allowed": allowed, "decision": decision, "reason": ""}, "step_decision")


@pytest.mark.parametrize(("context", "allowed", "decision", "same"), [
    (DecisionContext("s1", ResultKind.WORK, True, False, 0), ["accept", "review", "retry", "replan", "fail"],
     "accept", True),
    (DecisionContext("s1", ResultKind.WORK, False, True, 2, max_retries=2, open_gates=frozenset({"g1"})),
     ["replan", "fail"], "replan", True),
    (DecisionContext("s1", ResultKind.REVIEW, True, True, 1, adjudication_findings=(("seat", 0),)),
     ["accept", "retry", "replan", "fail"], "accept", True),
    # The model's choice must be one code allowed, and code must allow what it allowed then.
    (DecisionContext("s1", ResultKind.WORK, False, False, 2, max_retries=2), ["replan", "fail"], "retry", False),
    (DecisionContext("s1", ResultKind.WORK, False, False, 0), ["replan", "fail"], "fail", False),
])
def test_a_step_decision_replays_the_decisions_code_allowed(context, allowed, decision, same):
    result = replay(_decision_row(context, allowed, decision))
    assert result.same is same
    assert result.replayed == {"allowed": [d.value for d in context.allowed()]}
    assert all(Decision(value) for value in result.replayed["allowed"])


def test_context_without_evidence_keeps_its_fixed_baseline_json():
    # Recorded from ecc2eedbf2ac4a1b44ff9078e634002c228458c1, before evidence gating.
    before = {"step": "s1", "result": "work", "succeeded": True, "review_required": False,
              "retries": 0, "max_retries": 2, "last_open": False, "open_gates": None,
              "escalate": False, "verification_repair_required": False, "adjudication_findings": [],
              "advise": False, "checkpoint": False, "answered": False}
    context = DecisionContext("s1", ResultKind.WORK, True, False, 0)
    assert json.dumps(_context(context)) == json.dumps(before)
    assert _context_of(before) == context


@pytest.mark.parametrize(("passed", "decision"), [(False, "recheck"), (True, "accept")])
def test_evidence_snapshot_keeps_only_gate_state_and_replays_after_json(passed, decision):
    from looper.agents.acceptance_gate import Gate as EvidenceGate

    secret = "output and command text must stay out of snapshots"
    failed, errored, stale = ((), (), ()) if passed else (("c1",), ("c2",), ("c3",))
    evidence = EvidenceGate(passed, failed=failed, errored=errored, stale=stale,
                            rechecks_used=1, stale_reruns_used=2,
                            current=({"output_tail": secret, "command": secret, "opaque": object()},),
                            head=("measured-head", "measured-content"))
    context = DecisionContext("s1", ResultKind.WORK, True, False, 0, evidence=evidence)
    serialized = json.dumps(_context(context))
    assert secret not in serialized and "measured-head" not in serialized
    compact = json.loads(serialized)
    assert compact["evidence"] == {"passed": passed, "failed": list(failed), "errored": list(errored), "stale": list(stale),
                                    "rechecks_used": 1, "stale_reruns_used": 2}
    restored = _context_of(compact)
    assert isinstance(restored.evidence, EvidenceGate)
    assert restored.evidence.failed == failed and restored.evidence.errored == errored
    assert restored.evidence.stale == stale and restored.evidence.current == ()
    assert restored.offered(True) == context.offered(True)
    allowed = [item.value for item in context.offered(True)]
    row = _decision_row(context, allowed, decision)
    row["inputs"]["adjudicated_accept"] = True
    result = replay(row)
    assert result.same and result.replayed == {"allowed": allowed}
    if not passed:
        row["decision"]["decision"] = "accept"
        assert not replay(row).same


def test_a_turn_digest_keeps_hashes_flags_and_sizes_but_no_text():
    secret = "a prompt only the run record may keep"
    metadata = {
        "command": ["claude", "--print", "--model", "opus", "--disallowedTools", "Bash,Edit",
                    "--allowedTools", "Read,mcp__looper__*", "--append-system-prompt-file", "/runtime/p.md"],
        "system_prompt": secret, "system_prompt_sha256": "f" * 64,
        "prompt_layers": {"harness": {"source": "builtin_default", "sha256": "a" * 64},
                          "project": {"id": "proj-1", "background_sha256": "b" * 64, "knowledge_documents": [secret]},
                          "isolation": "v1"},
        "profile": {"id": "agent-1", "name": "Planner profile", "custom_prompt": secret,
                    "runtime": {"id": "rt-1", "name": "Claude Code", "type": "claude-code"}},
        "model_choice": {"model": "opus", "source": "builtin_default", "effort": "high"},
        "library": {"sha256": "c" * 64, "items": [{"name": secret}]},
        "read_only": "permissions",
    }
    digest = turn_digest({"id": 7, "metadata": metadata, "input_chars": 1200, "input_tokens": 900,
                          "context_tokens": 40000, "cache_read_tokens": 30000})
    assert digest == {
        "record_id": 7, "profile": "agent-1", "runtime": "claude-code", "model": "opus", "effort": "high",
        "system_prompt_sha256": "f" * 64,
        "prompt_layers": {"harness": {"sha256": "a" * 64}, "project": {"background_sha256": "b" * 64}},
        "tools": {"--disallowedTools": "Bash,Edit", "--allowedTools": "Read,mcp__looper__*"},
        "read_only": "permissions", "library_sha256": "c" * 64,
        "sizes": {"input_chars": 1200, "input_tokens": 900, "context_tokens": 40000, "cache_read_tokens": 30000},
    }
    assert secret not in json.dumps(digest)
