"""The Manager's session budget (assistant-layer RFC, section 5.4): the policy and the operator-only setting.
Runtime wiring lives in ``tiers.manager_budget``.

``manager.turn_budget_usd`` (a positive number of US dollars) turns it on; no row is off. When it is on, the
Manager will, after each turn, apply ``decide`` to that turn's context and cost, compact or rotate its session as
the decision says, cap each turn's spend at the budget (``turn_cap``) and answer a turn stopped there with
``over_cap_reply``. The context limit is the one setting resident sessions share with schedule Planners
(``scheduler.session_context_max_tokens``), and the bound is #1473's ``schedule_session.session_bound``: one
threshold and one cost rule, except that an unknown turn cost does not trigger the Manager's cost rule.

Everything here is a pure function of typed inputs, except the setting's read and write. The setting is the
operator's alone: the v1 operations ``session_budget.get`` and ``session_budget.update`` are operator-only, and no
Manager tool lists it.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
from typing import Any

from ..db import system_settings as db_settings
from .project_execution import format_usd
from .schedule_session import BoundReason, session_bound

TURN_BUDGET_KEY = "manager.turn_budget_usd"
FIELDS = ("turn_budget_usd",)
# The value to turn it on with (RFC decision D12): the live Manager's per-turn P99 over 30 days ($1.7887), doubled
# and rounded up to $0.50, at least $1. The setting itself stays unset (off) until the operator sets it.
RECOMMENDED_TURN_BUDGET_USD = 4.0


class Action(str, Enum):
    """What the Manager does with its session after a turn."""

    KEEP = "keep"
    COMPACT = "compact"  # a maintenance /compact on the resident session, then ``after_compaction``
    ROTATE = "rotate"  # the session-switch flow: the old session writes its handoff and a new one starts


class Outcome(str, Enum):
    """What was done to a session that reached its bound, as ``metadata.session_bound.action`` records it."""

    COMPACTED = "compacted"
    ROTATED = "rotated"


class TurnCap(str, Enum):
    """Whether a Manager turn's spend is capped at the budget."""

    OFF = "off"  # no budget set
    CAPPED = "capped"
    NO_PRICE = "no_price"  # a budget, but no price for the turn's model: no soft cap, and Settings says so


@dataclass(frozen=True)
class Decision:
    action: Action
    reason: BoundReason | None  # None when the session is kept
    before_tokens: int | None


KEEP = Decision(Action.KEEP, None, None)


@dataclass(frozen=True)
class Compaction:
    """How a maintenance ``/compact`` went: whether it finished, whether its stream had a ``compact_boundary``, and
    the context it reported after (``post_tokens``; None when it reported none)."""

    finished: bool
    boundary: bool
    post_tokens: int | None


def decide(context_tokens: int | None, last_turn_cost_usd: float | None, *, budget_usd: float | None,
           max_tokens: int, can_compact: bool) -> Decision:
    """What to do after a turn whose context ended at ``context_tokens`` and which cost ``last_turn_cost_usd``.

    - No budget: keep (off).
    - Context unknown: keep. The Manager measures every turn, so one turn its runtime did not measure is no reason
      to rotate (unlike a schedule's Planner, which runs rarely).
    - Over ``max_tokens``: compact when the runtime can compact a resident session headlessly, else rotate.
    - At least half of ``max_tokens`` and the known turn cost more than half the budget: rotate.
      Unlike #1473, an unknown cost skips the cost rule; context alone can still compact or rotate.
    """
    if budget_usd is None or context_tokens is None:
        return KEEP
    bound = session_bound(context_tokens, last_turn_cost_usd,
                          budget_usd if last_turn_cost_usd is not None else None,
                          max_tokens, reports_context=False)
    if bound is None:
        return KEEP
    if bound.reason is BoundReason.CONTEXT and can_compact:
        return Decision(Action.COMPACT, bound.reason, bound.before_tokens)
    return Decision(Action.ROTATE, bound.reason, bound.before_tokens)


def after_compaction(compaction: Compaction, *, max_tokens: int) -> Outcome:
    """Compacted when the exchange finished with a ``compact_boundary`` and the context it reported is within
    ``max_tokens``; rotated otherwise (it failed, its stream had no boundary, or the context is still over). A
    boundary without a reported size counts as compacted: the next turn measures the context again."""
    if not compaction.finished or not compaction.boundary:
        return Outcome.ROTATED
    if compaction.post_tokens is not None and compaction.post_tokens > max_tokens:
        return Outcome.ROTATED
    return Outcome.COMPACTED


def bound_metadata(outcome: Outcome, decision: Decision, after_tokens: int | None) -> dict[str, Any]:
    """``metadata.session_bound`` of the record that carries a compaction or a rotation."""
    if decision.reason is None:
        raise ValueError("a kept session has no bound to record")
    return {"action": outcome.value, "reason": decision.reason.value, "before_tokens": decision.before_tokens,
            "after_tokens": after_tokens}


def turn_cap(budget_usd: float | None, *, priced: bool) -> TurnCap:
    """Whether a Manager turn is capped at the budget. ``priced``: a price is known for the turn's model (the
    provider's, or the model catalog's for a run the runtime prices itself); without one there is no soft cap."""
    if budget_usd is None:
        return TurnCap.OFF
    return TurnCap.CAPPED if priced else TurnCap.NO_PRICE


def over_cap_reply(budget_usd: float) -> str:
    """The fixed reply to a turn its spend stopped at the budget (RFC 5.4)."""
    return (f"这一轮的花费到了单轮上限 ${format_usd(budget_usd)}，已经停下。已经做了的工具调用没有撤销。"
            "可以换个问法再问，或发 /new。")


# --- The setting -----------------------------------------------------------------------------------------------

def _budget(value: Any) -> float:
    """``value`` as a budget; ``ValueError`` unless it is a positive, finite number (not a boolean)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
        raise ValueError("turn_budget_usd must be a positive number of US dollars, or null")
    return float(value)


def _stored(raw: str | None) -> tuple[float | None, str | None]:
    """The stored text as a budget, and the problem with it when it is not one (the budget is then off)."""
    if raw is None:
        return None, None
    try:
        return _budget(float(raw)), None
    except ValueError:
        return None, f"the stored {TURN_BUDGET_KEY} {raw!r} is not a positive number; the budget is off"


async def turn_budget_usd(*, pool: Any = None) -> float | None:
    """The budget in effect: the stored value, or None (off) when there is none or it is not a positive number."""
    stored = await db_settings.get_settings([TURN_BUDGET_KEY], pool=pool)
    return _stored(stored.get(TURN_BUDGET_KEY))[0]


async def view(*, pool: Any = None) -> dict[str, Any]:
    """The setting as the operator sees it: its value (null: off), the value to turn it on with, and the problem
    with a stored value that is not a budget."""
    stored = await db_settings.get_settings([TURN_BUDGET_KEY], pool=pool)
    value, problem = _stored(stored.get(TURN_BUDGET_KEY))
    return {"turn_budget_usd": value, "recommended_usd": RECOMMENDED_TURN_BUDGET_USD, "problem": problem}


def checked(body: Mapping[str, Any]) -> dict[str, float | None]:
    """The fields of ``body``, each a budget or null (unset: off); ``ValueError`` for an unknown field or a bad
    value. A field the body leaves out keeps its value."""
    if unknown := [key for key in body if key not in FIELDS]:
        raise ValueError(f"unknown setting {unknown[0]!r}; the session budget has {', '.join(FIELDS)}")
    if not body:
        raise ValueError(f"give at least one of {', '.join(FIELDS)} (null: unset)")
    return {key: None if body[key] is None else _budget(body[key]) for key in FIELDS if key in body}


async def update(body: Mapping[str, Any], *, pool: Any = None) -> dict[str, Any]:
    """Store the fields of ``body`` and answer the setting as it is now; a refused body changes nothing."""
    values = checked(body)
    if "turn_budget_usd" in values:
        value = values["turn_budget_usd"]
        await db_settings.put_settings({TURN_BUDGET_KEY: None if value is None else repr(value)}, pool=pool)
    return await view(pool=pool)
