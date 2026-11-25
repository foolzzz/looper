"""Fail-closed model boundary for the default Manager's routed group and DM sessions.

NON_MAIN_ALLOWLIST is the complete grant, for both inputs and tools. New global
sections, contracts and tools do not inherit permission. Main (including every
turn while routing is off) and scenario Managers keep their existing assembly and authorization.
"""

from dataclasses import dataclass
from enum import Enum


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

PUBLIC_PROMPT = (
    "You are Looper's chat assistant for this conversation. Answer the current message using only this chat. "
    "You have no access to the operator's projects, tasks, settings, memories or other conversations, and no "
    "platform tools. Do not claim to read or change them. Ask the operator to use their own chat for those actions."
)


def for_session(key: str, *, manager_id: str) -> SessionAllowlist | None:
    """Only the default Manager's routed turns are restricted, including while routing drains."""
    from .managers_api import DEFAULT_MANAGER_ID

    return NON_MAIN_ALLOWLIST if manager_id == DEFAULT_MANAGER_ID and key != "main" else None
