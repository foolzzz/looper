"""The Manager's session budget rules (``agents.session_budget``): what a turn's context and cost decide, how a
compaction ends, the per-turn cap and its reply, the setting's checks, and that no agent sees any of it."""

import math

import pytest

from looper.agents import session_budget as budget
from looper.agents.schedule_session import BoundReason
from looper.agents.session_budget import Action, Compaction, Decision, Outcome, TurnCap

MAX = 100_000


@pytest.mark.parametrize("context,cost,budget_usd,can_compact,expected", [
    (MAX * 3, 9.0, None, True, budget.KEEP),  # off: whatever the turn measured
    (None, 9.0, 4.0, True, budget.KEEP),  # unmeasured: no action
    (MAX - 1, 9.0, 4.0, True, Decision(Action.ROTATE, BoundReason.COST, MAX - 1)),
    (MAX + 1, 0.1, 4.0, True, Decision(Action.COMPACT, BoundReason.CONTEXT, MAX + 1)),
    (MAX + 1, 0.1, 4.0, False, Decision(Action.ROTATE, BoundReason.CONTEXT, MAX + 1)),
    (MAX // 2, 2.01, 4.0, True, Decision(Action.ROTATE, BoundReason.COST, MAX // 2)),
    (MAX // 2, 2.0, 4.0, True, budget.KEEP),  # half the budget is not over it
    (MAX // 2, None, 4.0, True, Decision(Action.ROTATE, BoundReason.COST, MAX // 2)),  # #1473's rule
    (MAX // 2 - 1, 9.0, 4.0, True, budget.KEEP),  # a small context with a high cost
])
def test_what_a_turn_decides(context, cost, budget_usd, can_compact, expected):
    assert budget.decide(context, cost, budget_usd=budget_usd, max_tokens=MAX, can_compact=can_compact) == expected


@pytest.mark.parametrize("compaction,expected", [
    (Compaction(finished=True, boundary=True, post_tokens=MAX // 4), Outcome.COMPACTED),
    (Compaction(finished=True, boundary=True, post_tokens=MAX), Outcome.COMPACTED),
    (Compaction(finished=True, boundary=True, post_tokens=None), Outcome.COMPACTED),  # measured next turn
    (Compaction(finished=True, boundary=True, post_tokens=MAX + 1), Outcome.ROTATED),  # did not help
    (Compaction(finished=True, boundary=False, post_tokens=MAX // 4), Outcome.ROTATED),  # no compact_boundary
    (Compaction(finished=False, boundary=False, post_tokens=None), Outcome.ROTATED),  # it raised
])
def test_how_a_compaction_ends(compaction, expected):
    assert budget.after_compaction(compaction, max_tokens=MAX) is expected


def test_the_record_of_a_bound():
    decision = Decision(Action.COMPACT, BoundReason.CONTEXT, MAX + 5)
    assert budget.bound_metadata(Outcome.COMPACTED, decision, 20_000) == {
        "action": "compacted", "reason": "context", "before_tokens": MAX + 5, "after_tokens": 20_000}
    with pytest.raises(ValueError):
        budget.bound_metadata(Outcome.ROTATED, budget.KEEP, None)


@pytest.mark.parametrize("budget_usd,priced,expected", [
    (None, True, TurnCap.OFF), (4.0, True, TurnCap.CAPPED), (4.0, False, TurnCap.NO_PRICE)])
def test_the_per_turn_cap(budget_usd, priced, expected):
    assert budget.turn_cap(budget_usd, priced=priced) is expected


def test_the_over_cap_reply_is_the_rfc_text_with_the_budget():
    assert budget.over_cap_reply(4.0) == ("这一轮的花费到了单轮上限 $4.00，已经停下。已经做了的工具调用没有撤销。"
                                          "可以换个问法再问，或发 /new。")


@pytest.mark.parametrize("body,expected", [
    ({"turn_budget_usd": 4}, {"turn_budget_usd": 4.0}),
    ({"turn_budget_usd": 0.25}, {"turn_budget_usd": 0.25}),
    ({"turn_budget_usd": None}, {"turn_budget_usd": None}),
])
def test_a_budget_is_a_positive_number_or_null(body, expected):
    assert budget.checked(body) == expected


@pytest.mark.parametrize("body,problem", [
    ({"turn_budget_usd": 0}, "positive number"), ({"turn_budget_usd": -1}, "positive number"),
    ({"turn_budget_usd": math.inf}, "positive number"), ({"turn_budget_usd": math.nan}, "positive number"),
    ({"turn_budget_usd": True}, "positive number"), ({"turn_budget_usd": "4"}, "positive number"),
    ({}, "give at least one"), ({"turn_budget": 4}, "unknown setting"),
])
def test_anything_else_is_refused(body, problem):
    with pytest.raises(ValueError, match=problem):
        budget.checked(body)


def test_no_agent_tool_or_default_prompt_mentions_it():
    from looper.agents import agent_settings, api_v1
    from looper.agents.ops import clients, planner, room, service
    from looper.agents.tiers import manager, planner as planner_tier, worker

    tools = (*service.MANAGER_TOOLS, *clients.CLIENT_TOOLS, *planner.TOOLS, *planner.ROLE_TOOLS, *room.TOOLS)
    texts = [f"{tool.name} {tool.description} {tool.schema}" for tool in tools]
    texts += [manager.DEFAULT_SYSTEM_PROMPT, planner_tier.DEFAULT_SYSTEM_PROMPT, worker.DEFAULT_SYSTEM_PROMPT,
              agent_settings.DEFAULT_HARNESS_PROMPT]
    assert [text[:60] for text in texts if "turn_budget" in text or "session_budget" in text] == []
    for name in ("session_budget.get", "session_budget.update"):
        operation = next(op for op in api_v1.operations() if op.operation_id == name)
        assert operation.operator_only and operation.tool is None
