"""Review-specific aggregation; acceptance uses the shared condition evaluator."""
from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, Mapping

from .conditions import Condition, Leaf, State, StepOutcome, evaluate, leaves, parse


if TYPE_CHECKING:
    from .planner import PlanStep, TaskPlan


def acceptance(condition: Condition, seats: Mapping[str, str]) -> dict[str, Any]:
    if isinstance(condition, Leaf):
        return {"step": seats[condition.step], "on": "passed"}
    children = [acceptance(child, seats) for child in condition.of]
    return {"k_of": condition.k, "of": children} if condition.form == "k_of" else {condition.form: children}


def state(plan: TaskPlan, target: PlanStep, results: Mapping[str, dict[str, Any]] | None = None) -> State:
    results = results or {}
    return evaluate(parse(target.accept_when, "accept_when"), lambda sid: StepOutcome.from_step(
        SimpleNamespace(kind="review", status=plan.step(sid).status, result=results.get(sid, plan.step(sid).result))))


def blocking(result: Mapping[str, Any] | None) -> list[dict[str, Any]]:
    return [f for f in (result or {}).get("findings", []) if f.get("severity") == "blocking"]


def report(plan: TaskPlan, target: PlanStep) -> list[str]:
    required = leaves(parse(target.accept_when, "accept_when"))
    lines, locations = [], {}
    for seat in (s for s in plan.steps if s.reviews == target.step_id):
        result = seat.result or {}
        verdict = "pending" if seat.status != "verified" else "PASS" if StepOutcome.from_step(seat).passed is State.SATISFIED else "FAIL"
        lines.append(f"{seat.role or 'builtin'} ({'required' if seat.step_id in required else 'advisory'}): "
                     f"{verdict} — {len(blocking(result))} blocking")
        for index, finding in enumerate(result.get("findings", [])):
            if finding["severity"] == "blocking":
                locations.setdefault(finding["where"], []).append(
                    f"- {seat.role or 'builtin'} finding {index}: {finding['text']}")
    lines.append("Blocking findings by location")
    for where, findings in locations.items():
        lines.extend([where, *findings])
    return lines

