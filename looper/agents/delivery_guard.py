"""Pure delivery policy; the Manager decides when to apply it and performs the I/O.

No callbacks, model calls, channel sends or interim progress messages live here.
A disabled turn carries no debt and keeps the existing reply path.
"""

from dataclasses import dataclass, replace
from enum import Enum
from collections.abc import Set

from .ops import TurnOrigin
from .runtimes import Capability

FOLLOWUP_MESSAGE = (
    "[Looper] Your last turn ended without any reply to the user. Reply now with the result, "
    "or with what blocked you and what you need from them. Do not start new work."
)
REMINDER_MESSAGE = (
    "[Looper] You have made 6 tool calls without a word to the user. Tell them in one sentence "
    "where you are, then finish or say what blocks you."
)
FAILURE_NOTICE = "这次没拿到结果：管家连续几次都没有给出回复。可以再问一次；反复出现的话发 /new 换一个新会话。"
MAX_FOLLOWUPS = 2
SILENT_TOOLS = 6
MAX_REMINDERS = 2


class DeliveryDebt(str, Enum):
    OWED = "owed"
    PAID = "paid"
    EXEMPT = "exempt"


class TurnEnding(str, Enum):
    """Map the Manager's typed result/exception to an ending; never infer it from error text."""

    EMPTY = "empty"  # successful NoReply only
    REPLY = "reply"
    ERROR = "error"
    EXIT = "exit"
    STOP = "stop"
    OVER_CAP = "over_cap"
    TOOLS_DOWN = "tools_down"
    REFUSED = "refused"


class FollowupAction(str, Enum):
    KEEP = "keep"  # preserve the existing reply/fallback
    FOLLOWUP = "followup"
    FAILURE_NOTICE = "failure_notice"


class BlockKind(str, Enum):
    TEXT = "text"
    TOOL_USE = "tool_use"
    OTHER = "other"


@dataclass(frozen=True)
class GuardState:
    debt: DeliveryDebt | None
    silent_tools: int = 0
    reminders: int = 0


def initial_debt(*, enabled: bool, origin: TurnOrigin) -> DeliveryDebt | None:
    if not enabled:
        return None
    return DeliveryDebt.OWED if origin is TurnOrigin.USER else DeliveryDebt.EXEMPT


def followup_action(debt: DeliveryDebt | None, *, ending: TurnEnding, followups: int) -> FollowupAction:
    """``followups`` counts hidden turns already attempted, including one that failed.

    A stop always keeps normal stop semantics. Other follow-up failures stop retrying
    and use the fixed notice. Initial failures never start recovery.
    """
    if not 0 <= followups <= MAX_FOLLOWUPS:
        raise ValueError(f"followups must be between 0 and {MAX_FOLLOWUPS}")
    if debt is not DeliveryDebt.OWED or ending in (TurnEnding.REPLY, TurnEnding.STOP):
        return FollowupAction.KEEP
    if ending is TurnEnding.EMPTY:
        return FollowupAction.FOLLOWUP if followups < MAX_FOLLOWUPS else FollowupAction.FAILURE_NOTICE
    return FollowupAction.FAILURE_NOTICE if followups else FollowupAction.KEEP


def observe_block(state: GuardState, kind: BlockKind, *, text: str = "", top_level: bool = True,
                  capabilities: Set[Capability] = frozenset()) -> tuple[GuardState, str | None]:
    """Consume one complete block, once. Nested tool/agent content never pays a debt.

    The returned reminder is an input to the same running turn, not a channel send.
    Counting resets on text and on a reminder; the per-turn reminder cap never resets.
    """
    if not top_level or state.debt in (None, DeliveryDebt.EXEMPT):
        return state, None
    if kind is BlockKind.TEXT and text.strip():
        return replace(state, debt=DeliveryDebt.PAID, silent_tools=0), None
    if (kind is not BlockKind.TOOL_USE or Capability.MID_TURN_INPUT not in capabilities
            or state.reminders >= MAX_REMINDERS):
        return state, None
    silent = state.silent_tools + 1
    if silent >= SILENT_TOOLS:
        return replace(state, silent_tools=0, reminders=state.reminders + 1), REMINDER_MESSAGE
    return replace(state, silent_tools=silent), None
