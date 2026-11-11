"""Reasoned downgrades of findings in the current review round."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from typing import TYPE_CHECKING, Any

from .conditions import Condition, Leaf, StepOutcome, parse
from .planner_protocol import ProtocolError
from .review_panel import State, blocking


if TYPE_CHECKING:
    from .planner import PlanStep, TaskPlan


def can_accept(plan: TaskPlan, target: PlanStep) -> bool:
    """Whether downgrading eligible findings alone can satisfy acceptance, without changing the plan."""
    return bool(required_downgrades(plan, target))


def required_downgrades(plan: TaskPlan, target: PlanStep) -> tuple[tuple[str, int], ...] | None:
    """A smallest sufficient set from the saved round; refused replies never change that round.

    Conditions cannot repeat seats, so the cheapest k children also minimize the total
    number of findings. Ties keep the acceptance rule's order.
    """
    proposals = [
        {"member": seat.role or "builtin", "finding": index, "level": "suggestion", "reason": "Eligibility check only"}
        for seat in plan.steps if seat.reviews == target.step_id and seat.status == "verified"
        and not (seat.result or {}).get("refused") and not (seat.result or {}).get("failure")
        for index, finding in enumerate((seat.result or {}).get("findings", []))
        if finding.get("severity") == "blocking"
    ]
    results = adjudicate(plan, target, proposals) if proposals else {}

    def choose(condition: Condition) -> tuple[tuple[str, int], ...] | None:
        if isinstance(condition, Leaf):
            seat = plan.step(condition.step)
            if StepOutcome.from_step(seat).for_on(condition.on) is State.SATISFIED:
                return ()
            if seat.step_id not in results or StepOutcome.from_step(
                    replace(seat, result=results[seat.step_id])).for_on(condition.on) is not State.SATISFIED:
                return None
            return tuple((seat.role or "builtin", index)
                         for index, finding in enumerate((seat.result or {}).get("findings", []))
                         if finding.get("severity") == "blocking")
        choices = [chosen for child in condition.of if (chosen := choose(child)) is not None]
        if len(choices) < condition.k:
            return None
        return tuple(finding for chosen in sorted(choices, key=len)[:condition.k] for finding in chosen)

    return choose(parse(target.accept_when, "accept_when"))


def adjudicate(plan: TaskPlan, target: PlanStep, proposals: Any) -> dict[str, dict[str, Any]]:
    if not isinstance(proposals, (tuple, list)) or not proposals:
        raise ProtocolError("adjudications must be a nonempty list")
    seats = {s.role or "builtin": s for s in plan.steps if s.reviews == target.step_id}
    results, seen = {}, set()
    for item in proposals:
        if not isinstance(item, dict) or set(item) != {"member", "finding", "level", "reason"}:
            raise ProtocolError("adjudications need member, finding, level and reason")
        member, index, reason = item["member"], item["finding"], item["reason"]
        if (not isinstance(member, str) or member not in seats or type(index) is not int
                or item["level"] != "suggestion" or not isinstance(reason, str)
                or not reason.strip() or len(reason) > 500 or (member, index) in seen):
            raise ProtocolError("adjudication must name a current blocking finding with a reason of 1..500 characters")
        seat = seats[member]
        result = results.setdefault(seat.step_id, deepcopy(seat.result or {}))
        findings = result.get("findings", [])
        if (seat.status != "verified" or not 0 <= index < len(findings)
                or findings[index]["severity"] != "blocking" or result.get("refused") or result.get("failure")):
            raise ProtocolError("adjudication must reference a blocking finding in this round")
        seen.add((member, index))
        findings[index] = {**findings[index], "severity": "suggestion", "reason": reason.strip()}
        if not blocking(result):
            result["status"] = "passed_by_adjudication"
    return results
