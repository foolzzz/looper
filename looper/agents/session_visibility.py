"""Fail-closed model boundary for routed and scoped chat sessions.

NON_MAIN_ALLOWLIST is the complete grant, for both inputs and tools. New global
sections, contracts and tools do not inherit permission. Main (including every
turn while routing is off) and unscoped scenario sessions keep their existing assembly and authorization.
"""

from collections.abc import Mapping
from dataclasses import dataclass, replace
from enum import Enum
from types import MappingProxyType

from . import session_tools_settings
from .ops.session_tools import (ChatTaskSummary, ChatTaskDetail, ChatWriteOutcome, ChatControlOutcome,
                                ChatInstructOutcome)


class Input(str, Enum):
    PUBLIC_PROMPT = "public_prompt"
    PERSONA_NAME = "persona_name"
    CHAT_OPENING = "chat_opening"
    CHAT_HISTORY = "chat_history"
    CHAT_HANDOFF = "chat_handoff"
    MESSAGE = "message"
    PROVENANCE = "provenance"
    QUOTES = "quotes"
    ATTACHMENTS = "attachments"
    STOP_NOTICE = "stop_notice"
    STABLE_BLOCK = "stable_block"


@dataclass(frozen=True)
class SessionAllowlist:
    version: int
    inputs: frozenset[Input]
    tools: frozenset[str]


NON_MAIN_ALLOWLIST = SessionAllowlist(
    version=1,
    inputs=frozenset({Input.PUBLIC_PROMPT, Input.PERSONA_NAME, Input.CHAT_OPENING,
                      Input.CHAT_HISTORY, Input.CHAT_HANDOFF, Input.MESSAGE, Input.PROVENANCE,
                      Input.QUOTES, Input.ATTACHMENTS, Input.STOP_NOTICE, Input.STABLE_BLOCK}),
    # No existing Manager tool has a complete chat scope, including its return
    # value and error paths. No tools are registered or callable until one does.
    tools=frozenset(),
)

SCOPED_CHAT_ALLOWLIST = SessionAllowlist(version=1, inputs=NON_MAIN_ALLOWLIST.inputs, tools=frozenset())
_SCOPED: dict[tuple[str, str], SessionAllowlist] = {}


def register_scoped(manager_id: str, key: str, allowlist: SessionAllowlist) -> None:
    _SCOPED[manager_id, key] = allowlist


def forget_scoped(manager_id: str, key: str) -> None:
    _SCOPED.pop((manager_id, key), None)


def scoped_for(manager_id: str, key: str) -> SessionAllowlist | None:
    return _SCOPED.get((manager_id, key))


PUBLIC_PROMPT = (
    "You are Looper's chat assistant for this conversation. Answer the current message using only this chat. "
    "You have no access to the operator's projects, tasks, settings, memories or other conversations, and no "
    "platform tools. Do not claim to read or change them. Ask the operator to use their own chat for those actions."
)


class IdKind(str, Enum):
    TASK = "task"
    SCHEDULE = "schedule"


class WriteGate(str, Enum):
    RUN = "run"
    APPROVAL = "approval"


@dataclass(frozen=True)
class SessionToolRule:
    ids: tuple[IdKind, ...]
    view: tuple[type, ...]
    operator: WriteGate | None
    other: WriteGate | None


SESSION_TOOL_RULES: Mapping[str, SessionToolRule] = MappingProxyType({
    "looper_chat_tasks": SessionToolRule((IdKind.TASK,), (ChatTaskSummary, ChatTaskDetail), None, None),
    "looper_chat_task_create": SessionToolRule((), (ChatWriteOutcome,), WriteGate.RUN, WriteGate.APPROVAL),
    "looper_chat_task_control": SessionToolRule((IdKind.TASK,), (ChatControlOutcome,), WriteGate.RUN, WriteGate.APPROVAL),
    "looper_chat_task_instruct": SessionToolRule((IdKind.TASK,), (ChatInstructOutcome,), WriteGate.RUN, WriteGate.APPROVAL),
})
NON_MAIN_WITH_TOOLS = replace(NON_MAIN_ALLOWLIST, tools=frozenset(SESSION_TOOL_RULES))
PUBLIC_PROMPT_TOOLS = (
    "You are Looper's chat assistant for this conversation. Use only this chat and its chat task tools.\n"
    "Read only tasks belonging to this chat; private requirements, configuration and progress are unavailable.\n"
    "The operator may create tasks in existing projects, start, stop, restart and send instructions here.\n"
    "Other senders cannot write yet; cancellation and starting over require the operator's own chat.\n"
    "Never ask this chat to confirm a refused action or claim it ran. You have no shell or file tools."
)


def for_session(key: str, *, manager_id: str) -> SessionAllowlist | None:
    """Scoped grants take precedence; otherwise preserve the default Manager's routed boundary."""
    from .managers_api import DEFAULT_MANAGER_ID

    if (scoped := scoped_for(manager_id, key)) is not None:
        return scoped
    if manager_id != DEFAULT_MANAGER_ID or key == "main":
        return None
    return NON_MAIN_WITH_TOOLS if session_tools_settings.enabled() else NON_MAIN_ALLOWLIST
