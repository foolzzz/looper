"""With the project switch ``advisor`` off (the default), what the Planner and its Workers are given stays as it was
before the advisor (the advisor RFC, section 13): the reply contract in every combination of its options, the
DECISION messages of every kind of result, the Worker's first turn and task section, and the contract's refusals.

The digests below were computed before the advisor's Planner side existed; turning the switch on must change them.
"""

from __future__ import annotations

import hashlib
import itertools
import json
from pathlib import Path
from typing import Any

import pytest

from looper.agents.tiers import planner as p
from looper.agents.tiers.planner_protocol import (
    DecisionContext, PlanView, ProtocolError, ResultKind, parse_decision, parse_plan, reply_contract,
)
from looper.agents.tiers.worker import WorkerPool

CONTRACT_SHA256 = "e67f24b0f185d56fea945435d72d15c8a2d88de2d1126cdb6bdcea66aeb233cc"
DECISIONS_SHA256 = "d75c9b1cf1c0389e8490be6cdd77ff571c9244d5d814f1e84df2167377c94807"
WORKER_SHA256 = "f2d8e4a1652f6323879950902bd875ea4245ed32c4b61c61730ccc158a27cc9c"
REFUSALS_SHA256 = "1384050dfc66b745ecbfc588c67dffd6d41347494b1c4c71ffe4db796cc44008"

_FLAGS = ("roles", "gates", "room", "conditions", "review_panel", "adjudication", "wait_commands")


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def _contracts(**extra: Any) -> dict[str, str]:
    seen = {}
    for values in itertools.product((False, True), repeat=len(_FLAGS)):
        flags = dict(zip(_FLAGS, values))
        seen[json.dumps(flags, sort_keys=True)] = reply_contract(3, **flags, **extra)
    return seen


def _planner(tmp_path: Path) -> p.PlannerAgent:
    agent = p.PlannerAgent(task_id="task-off", runtime_root=tmp_path)
    agent._plan = p.TaskPlan("plan", "goal", "Speed up the importer", str(tmp_path), steps=[
        p.PlanStep("step-a", 0, "Profile the import loop.", key="s1", acceptance_criteria=["a profile is attached"]),
        p.PlanStep("step-b", 1, "Rewrite the loop.", key="s2", depends_on=["step-a"], review=True),
    ])
    return agent


_CONTEXTS = {
    "work_ok": DecisionContext("s1", ResultKind.WORK, True, False, 0),
    "work_ok_review": DecisionContext("s1", ResultKind.WORK, True, True, 0),
    "work_failed": DecisionContext("s1", ResultKind.WORK, False, False, 1, max_retries=3),
    "work_failed_last_retry": DecisionContext("s1", ResultKind.WORK, False, False, 3, max_retries=3),
    "review_failed": DecisionContext("s2", ResultKind.REVIEW, False, True, 0),
    "review_passed": DecisionContext("s2", ResultKind.REVIEW, True, True, 0),
    "escalate": DecisionContext("s1", ResultKind.WORK, False, False, 3, max_retries=3, escalate=True),
    "last_open": DecisionContext("s2", ResultKind.WORK, False, False, 0, last_open=True),
}


def _decisions(agent: p.PlannerAgent) -> dict[str, str]:
    plan = agent._plan
    return {name: agent._decision_message(plan, plan.steps[1] if context.step == "s2" else plan.steps[0], context,
                                          ["Worker: the loop still reads every key twice."])
            for name, context in _CONTEXTS.items()}


def _worker() -> dict[str, str]:
    payloads = {
        "first": {"task_id": "step-a", "step_key": "s1", "instruction": "Profile the loop.", "requirement": "Go fast",
                  "acceptance_criteria": ["a profile"], "attempt": 1},
        "retry": {"task_id": "step-a", "step_key": "s1", "instruction": "Profile the loop.", "requirement": "Go fast",
                  "acceptance_criteria": ["a profile"], "attempt": 2, "feedback": ["Measure the second read."],
                  "dependencies": [{"step": "s0", "summary": "Found the loop.", "changed_files": ["src/loop.py"]}],
                  "findings": "## Findings\n- the store's tail is slow"},
    }
    worker = WorkerPool.__new__(WorkerPool)
    seen = {}
    for name, payload in payloads.items():
        seen[f"{name}/turn"] = worker._build_work_prompt(payload)
        seen[f"{name}/task"] = WorkerPool._task_section(payload, "/work", is_verify=False)
    return seen


def _refusals() -> dict[str, str]:
    view = PlanView(started={"s1": ()}, pending={"s2": ("s1",)})
    context = _CONTEXTS["work_failed"]
    replies = {
        "advise": {"decision": "advise", "advice_request": {"v": "advice_request/1"}},
        "proceed": {"decision": "proceed"},
        "adoption": {"decision": "retry", "feedback": "x", "advice_adoption": {"adoption": "adopted"}},
        "unknown": {"decision": "maybe"},
    }
    seen = {}
    for name, reply in replies.items():
        try:
            parse_decision(f"```json\n{json.dumps(reply)}\n```", context, view)
            seen[name] = "accepted"
        except ProtocolError as exc:
            seen[name] = str(exc)
    try:
        parse_plan('```json\n{"steps": [{"id": "s1", "instruction": "x", "acceptance_criteria": [], '
                   '"depends_on": [], "review": false, "difficulty": "critical"}]}\n```')
        seen["plan_difficulty"] = "accepted"
    except ProtocolError as exc:
        seen["plan_difficulty"] = str(exc)
    return seen


def _normalized(value: dict[str, str], tmp_path: Path) -> dict[str, str]:
    return {key: text.replace(str(tmp_path), "<tmp>") for key, text in value.items()}


def test_the_reply_contract_stays_as_it_was_without_the_advisor() -> None:
    assert _digest(_contracts()) == CONTRACT_SHA256


def test_decision_messages_stay_as_they_were_without_the_advisor(tmp_path: Path) -> None:
    assert _digest(_normalized(_decisions(_planner(tmp_path)), tmp_path)) == DECISIONS_SHA256


def test_a_project_that_had_consults_and_turned_the_advisor_off_gives_the_planner_the_same_messages(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The history a project's consults left (steps marked critical, a retry that took advice, earlier consults on
    record) changes nothing the Planner is given once its project turns the advisor off: no offer, no repeat
    check, the same DECISION messages."""
    import asyncio

    from looper.db import advisor_advice

    async def earlier(step_id: str) -> list[dict[str, Any]]:
        return [{"source": "checkpoint", "attempt": 1, "adoption": "adopted",
                 "advice": {"answers": [{"conclusion": "Read each key once."}]}}]

    monkeypatch.setattr(advisor_advice, "for_step", earlier)
    agent = _planner(tmp_path)
    for step in agent._plan.steps:
        step.difficulty, step.consult = "critical", 3
    assert not agent._execution.advisor_on
    assert _digest(_normalized(_decisions(agent), tmp_path)) == DECISIONS_SHA256

    async def main() -> None:
        assert await agent._advice_offer(agent._plan, agent._plan.steps[0]) is None
        assert await agent._advice_offer(agent._plan, None) is None
        parse = object()
        assert await agent._asking(agent._plan, agent._plan.steps[0], parse) is parse

    asyncio.run(main())


def test_a_workers_first_turn_and_task_stay_as_they_were_without_advice() -> None:
    assert _digest(_worker()) == WORKER_SHA256


def test_the_contracts_refusals_stay_as_they_were_without_the_advisor() -> None:
    assert _digest(_refusals()) == REFUSALS_SHA256


def test_with_the_advisor_on_each_part_changes(tmp_path: Path) -> None:
    from looper.agents.tiers.planner_advice import AdviceBudget, offer_lines

    off, on = _contracts(), _contracts(advisor=True)
    assert all(on[key] != off[key] and "[ADVICE]" in on[key] and "advice_adoption" in on[key] for key in off)
    agent = _planner(tmp_path)
    plan = agent._plan
    offer = offer_lines(AdviceBudget(1.0, 2, 10, 5.0), [])
    for name in ("work_failed", "review_failed"):
        context = _CONTEXTS[name]
        step = plan.steps[1] if context.step == "s2" else plan.steps[0]
        assert agent._decision_message(plan, step, context, ["Worker: x"], advice=offer) != \
            agent._decision_message(plan, step, context, ["Worker: x"])
    worker = WorkerPool.__new__(WorkerPool)
    payload = {"task_id": "step-a", "step_key": "s1", "instruction": "Profile the loop.", "attempt": 2}
    # The Worker never gets the advisor's words: a consult only names itself on its record.
    assert worker._build_work_prompt({**payload, "advisor_consult": 3}) == worker._build_work_prompt(payload)
    view = PlanView(started={"s1": ()}, pending={"s2": ("s1",)}, advisor=True)
    reply = parse_plan('```json\n{"steps": [{"id": "s1", "instruction": "x", "acceptance_criteria": [], '
                       '"depends_on": [], "review": false, "difficulty": "critical"}]}\n```', advisor=True)
    assert reply.steps[0].difficulty == "critical"
    from dataclasses import replace

    advise = parse_decision('```json\n{"decision": "advise", "advice_request": {"v": "advice_request/1"}}\n```',
                            replace(_CONTEXTS["work_failed"], advise=True), view)
    assert advise.advice_request == {"v": "advice_request/1"}
