"""Typed reasons for bounding a schedule's persistent Planner conversation."""

from dataclasses import dataclass
from enum import Enum


class BoundReason(str, Enum):
    CONTEXT = "context"
    COST = "cost"


@dataclass(frozen=True)
class SessionBound:
    reason: BoundReason
    before_tokens: int | None


def session_bound(context_tokens: int | None, run_cost_usd: float | None, budget_usd: float | None,
                  max_tokens: int) -> SessionBound | None:
    # A stored session without measurements must not get one more unbounded resume after an upgrade.
    if context_tokens is None or context_tokens > max_tokens:
        return SessionBound(BoundReason.CONTEXT, context_tokens)
    if (context_tokens >= max_tokens / 2 and budget_usd is not None
            and (run_cost_usd is None or run_cost_usd > budget_usd / 2)):
        return SessionBound(BoundReason.COST, context_tokens)
    return None
