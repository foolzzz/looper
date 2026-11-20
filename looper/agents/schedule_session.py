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
    if context_tokens is not None and context_tokens > max_tokens:
        return SessionBound(BoundReason.CONTEXT, context_tokens)
    if budget_usd is not None and run_cost_usd is not None and run_cost_usd > budget_usd / 2:
        return SessionBound(BoundReason.COST, context_tokens)
    return None
