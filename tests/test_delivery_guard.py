"""Delivery policy without a running Manager or channel side effects."""

import pytest

from looper.agents.delivery_guard import (
    BlockKind, DeliveryDebt as Debt, FollowupAction as Action, GuardState,
    TurnEnding as Ending, followup_action, initial_debt, observe_block,
    FAILURE_NOTICE, FOLLOWUP_MESSAGE, REMINDER_MESSAGE,
)
from looper.agents.ops import Turn, TurnOrigin
from looper.agents.runtimes import Capability


@pytest.mark.parametrize("enabled,origin,expected", [
    (False, TurnOrigin.USER, None), (False, TurnOrigin.COMPLETION, None),
    (True, TurnOrigin.USER, Debt.OWED), (True, TurnOrigin.OWN, Debt.EXEMPT),
    (True, TurnOrigin.COMPLETION, Debt.EXEMPT),
])
def test_initial_debt(enabled, origin, expected):
    assert initial_debt(enabled=enabled, origin=origin) is expected


@pytest.mark.parametrize("debt,ending,followups,expected", [
    (None, Ending.EMPTY, 0, Action.KEEP), (Debt.EXEMPT, Ending.EMPTY, 0, Action.KEEP),
    (Debt.PAID, Ending.EMPTY, 0, Action.KEEP), (Debt.OWED, Ending.REPLY, 0, Action.KEEP),
    (Debt.OWED, Ending.EMPTY, 0, Action.FOLLOWUP), (Debt.OWED, Ending.EMPTY, 1, Action.FOLLOWUP),
    (Debt.OWED, Ending.EMPTY, 2, Action.FAILURE_NOTICE),
    *[(Debt.OWED, ending, 0, Action.KEEP) for ending in
      (Ending.ERROR, Ending.EXIT, Ending.STOP, Ending.OVER_CAP, Ending.TOOLS_DOWN, Ending.REFUSED)],
    (Debt.OWED, Ending.ERROR, 1, Action.FAILURE_NOTICE),
    (Debt.OWED, Ending.STOP, 1, Action.KEEP),
])
def test_followup_policy(debt, ending, followups, expected):
    assert followup_action(debt, ending=ending, followups=followups) is expected


@pytest.mark.parametrize("debt,capabilities", [
    (None, frozenset({Capability.MID_TURN_INPUT})),
    (Debt.EXEMPT, frozenset({Capability.MID_TURN_INPUT})), (Debt.OWED, frozenset()),
])
def test_ineligible_turns_never_remind(debt, capabilities):
    state = GuardState(debt)
    for _ in range(20):
        state, reminder = observe_block(state, BlockKind.TOOL_USE, capabilities=capabilities)
        assert reminder is None


def test_only_top_level_text_pays_and_resets_the_silent_streak():
    caps = frozenset({Capability.MID_TURN_INPUT})
    state = GuardState(Debt.OWED, silent_tools=5)
    for kind, text, top_level in [(BlockKind.TEXT, "nested", False), (BlockKind.TEXT, "", True),
                                   (BlockKind.OTHER, "ignored", True), (BlockKind.TOOL_USE, "", False)]:
        unchanged, reminder = observe_block(state, kind, text=text, top_level=top_level, capabilities=caps)
        assert unchanged == state and reminder is None
    state, reminder = observe_block(state, BlockKind.TEXT, text="result", capabilities=caps)
    assert state == GuardState(Debt.PAID) and reminder is None


def test_six_tools_remind_at_most_twice_and_text_resets_count():
    caps = frozenset({Capability.MID_TURN_INPUT})
    state = GuardState(Debt.OWED)
    writes = []
    for index in range(24):
        state, reminder = observe_block(state, BlockKind.TOOL_USE, capabilities=caps)
        if reminder:
            writes.append((index + 1, reminder))
        if index == 5:
            state, _ = observe_block(state, BlockKind.TEXT, text="progress", capabilities=caps)
    assert writes == [(6, REMINDER_MESSAGE), (12, REMINDER_MESSAGE)]
    assert state.debt is Debt.PAID and state.reminders == 2


def test_fixed_text_is_the_rfc_contract():
    assert FOLLOWUP_MESSAGE == ("[Looper] Your last turn ended without any reply to the user. Reply now with the "
                                "result, or with what blocked you and what you need from them. Do not start new work.")
    assert REMINDER_MESSAGE == ("[Looper] You have made 6 tool calls without a word to the user. Tell them in one "
                                "sentence where you are, then finish or say what blocks you.")
    assert FAILURE_NOTICE == "这次没拿到结果：管家连续几次都没有给出回复。可以再问一次；反复出现的话发 /new 换一个新会话。"


def test_ops_turn_is_inert_until_explicitly_initialized():
    assert Turn("web", None, message_id="synthetic").debt is None


@pytest.mark.parametrize("followups", [-1, 3])
def test_invalid_followup_count_fails_fast(followups):
    with pytest.raises(ValueError, match="followups"):
        followup_action(Debt.OWED, ending=Ending.EMPTY, followups=followups)


@pytest.mark.parametrize("exc,expected", [
    (None, Ending.REPLY),
    ("empty", Ending.EMPTY),
    ("over_cap", Ending.OVER_CAP),
    ("exit", Ending.EXIT),
    ("tools_down", Ending.TOOLS_DOWN),
    ("refused", Ending.REFUSED),
    ("stopped", Ending.STOP),
    ("failed", Ending.ERROR),
    (RuntimeError("NoReply over_cap stopped"), Ending.ERROR),
])
def test_exception_endings(exc, expected):
    from looper.agents.delivery_guard import ending_for, OverCap, TurnExited, ToolsDown, TokenRefused
    from looper.agents.tiers.manager import NoReply, TurnFailed
    from looper.agents.tiers.turn_token import TurnStopped

    exceptions = {"empty": NoReply(), "over_cap": OverCap(), "exit": TurnExited(),
                  "tools_down": ToolsDown(), "refused": TokenRefused(),
                  "stopped": TurnStopped(), "failed": TurnFailed("failed", "unavailable")}
    assert ending_for(exceptions.get(exc, exc)) is expected
