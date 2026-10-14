"""When a resident agent's session cannot go on: the seam for what its new session is told.

The Manager and each task's Planner keep one session and resume it after a
restart. A session resumes only while its agent keeps the same runtime type
and the same provider (``runtimes.can_resume``, RFC P5); when either changes,
the agent's next turn starts a new session. Before it does, the agent hands a
``SessionSwitch`` to every hook registered here (``on_session_switch``) and
puts what the hooks return in front of that turn's message. Hooks decide what
a new session is told (a handoff summary, a resume brief); the tiers only
decide when a session switches and where the text goes.

A hook runs once per switch, in the order registered, and is awaited before the
new session's first turn. Its failure is logged and skipped: a switch never
fails the turn.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .agent_settings import Launch

logger = logging.getLogger(__name__)


class SwitchReason(str, Enum):
    """What changed: the runtime type (whatever happened to the provider), or only the provider."""

    RUNTIME = "runtime"
    PROVIDER = "provider"


@dataclass(frozen=True)
class SessionOwner:
    """What a session belongs to: a runtime type, a provider and the agent (``agents.id``); None where a session
    saved before it was recorded does not say."""

    runtime_type: str | None
    provider_id: str | None
    agent_id: str | None


@dataclass(frozen=True)
class SessionSwitch:
    """A resident agent's saved session that its current agent cannot resume.

    ``agent`` is the resident agent (``manager`` or ``planner-<task_id>``),
    ``task_id`` its task (a Planner's), ``session_id`` the session left behind,
    ``previous`` what it belonged to and ``current`` what the new session will.
    """

    agent: str
    task_id: str | None
    session_id: str
    reason: SwitchReason
    previous: SessionOwner
    current: SessionOwner

    def change(self) -> str:
        """What changed, in a few words: ``runtime claude-code -> codex`` or ``provider <id> -> <id>``."""
        if self.reason is SwitchReason.RUNTIME:
            return f"runtime {self.previous.runtime_type or 'claude-code'} -> {self.current.runtime_type}"
        return f"provider {self.previous.provider_id or 'prov-claude-subscription'} -> {self.current.provider_id}"


# Returns text for the new session's first turn, or None for nothing.
SessionSwitchHook = Callable[[SessionSwitch], Awaitable[str | None]]

_hooks: list[SessionSwitchHook] = []


def on_session_switch(hook: SessionSwitchHook) -> Callable[[], None]:
    """Register ``hook`` for every session switch; returns what unregisters it."""
    _hooks.append(hook)

    def _remove() -> None:
        if hook in _hooks:
            _hooks.remove(hook)

    return _remove


async def session_switched(switch: SessionSwitch) -> str:
    """Hand ``switch`` to every hook; what they return, in order, blank-line separated ('' for nothing)."""
    texts: list[str] = []
    for hook in list(_hooks):
        try:
            text = await hook(switch)
        except Exception:
            logger.warning("%s: a session switch hook failed", switch.agent, exc_info=True)
            continue
        if text and text.strip():
            texts.append(text.strip())
    return "\n\n".join(texts)


def session_switch(agent: str, task_id: str | None, session_id: str | None, previous: SessionOwner,
                   launch: Launch) -> SessionSwitch | None:
    """The switch ``agent`` makes when ``launch``'s agent cannot resume its saved session ``session_id``, which
    belongs to ``previous``; None when there is no saved session or it can be resumed."""
    from .runtimes import CLAUDE_CODE, can_resume

    runtime, provider = launch.runtime, launch.agent.provider
    if not session_id or can_resume(previous.runtime_type, previous.provider_id, runtime, provider.id):
        return None
    reason = SwitchReason.RUNTIME if (previous.runtime_type or CLAUDE_CODE.type) != runtime.type \
        else SwitchReason.PROVIDER
    return SessionSwitch(agent, task_id, session_id, reason, previous,
                         SessionOwner(runtime.type, provider.id, launch.agent.id))
