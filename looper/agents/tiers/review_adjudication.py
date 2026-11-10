"""Reasoned downgrades of findings in the current review round."""
from __future__ import annotations

from copy import deepcopy
from typing import TYPE_CHECKING, Any

from .planner_protocol import ProtocolError
from .review_panel import blocking


if TYPE_CHECKING:
    from .planner import PlanStep, TaskPlan


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
