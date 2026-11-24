"""What a call of a tool only the user's own turns run is told in any other turn: each tool's own words, declared
on the tool (``RestrictedTool.refusal``), never another tool's."""

from __future__ import annotations

import pytest

from looper.agents.ops import Audience, RestrictedTool, Risk
from looper.agents.ops.service import _OPERATOR_TOOLS


async def _never(call):  # pragma: no cover - never called
    raise AssertionError


def test_each_operator_only_tool_says_what_it_is_refused_for_in_its_own_words():
    assert {name: tool.refusal for name, tool in _OPERATOR_TOOLS.items()} == {
        "looper_intents_recognize": "intents are recognized on request only in the user's own direct chat or on "
                                   "the dashboard",
        "looper_leads_search": "message leads are searched only in the user's own direct chat or on the dashboard",
        "looper_vault_search": "the user's knowledge vault is searched and read only in their own direct chat or on "
                               "the dashboard",
        "looper_vault_read": "the user's knowledge vault is searched and read only in their own direct chat or on "
                             "the dashboard",
        "looper_session_search": "the user's earlier conversations are searched only in their own direct chat or on "
                                 "the dashboard",
        "looper_reminder_create": "looper_reminder_create runs only in the user's own direct chat or on "
                                  "the dashboard",
        "looper_reminder_list": "looper_reminder_list runs only in the user's own direct chat or on "
                                  "the dashboard",
        "looper_reminder_cancel": "looper_reminder_cancel runs only in the user's own direct chat or on "
                                  "the dashboard",
        "looper_task_hold": "Failure holds are controlled only by the operator in the console or their direct chat.",
    }


def test_a_tool_only_some_turns_run_cannot_be_declared_without_its_refusal():
    with pytest.raises(ValueError, match="looper_example runs only in some turns"):
        RestrictedTool(name="looper_example", description="d", schema={"type": "object"}, risk=Risk.READ,
                       run=_never, audience=Audience.OPERATOR)
    with pytest.raises(ValueError):
        RestrictedTool(name="looper_example", description="d", schema={"type": "object"}, risk=Risk.READ,
                       run=_never, audience=Audience.OPERATOR, refusal="  ")
    # A tool every turn runs needs none.
    assert RestrictedTool(name="looper_example", description="d", schema={"type": "object"}, risk=Risk.READ,
                          run=_never).refusal == ""
