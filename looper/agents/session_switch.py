"""Why a resident agent starts a new session instead of resuming its saved one (multi-runtime RFC P5).

The Manager and a task's Planner each keep one session and resume it after a
restart. Their next turn starts a new session instead when:

- ``AGENT_CHANGED``: the agent the turn runs cannot resume the saved session.
  ``agent_switch`` decides it before every turn, from what the session was
  saved with and the agent the turn resolved. It is the one place the rule
  lives: today another runtime type cannot resume a session
  (``runtimes.can_resume``); the provider rule (any change of provider starts
  a new session) belongs here too, with the provider saved beside the session.
- ``SESSION_LOST``: the runtime could not resume the saved session (it no
  longer exists), or there is none to resume for work under way.

Before an ``AGENT_CHANGED`` switch, an old session whose process still runs
takes one handoff turn: it writes for the next session what that one cannot
read back (its understanding of the code, unfinished reasoning, open risks).
The new session's first turn then gets a resume brief: why it is new, the
handoff, and the state the owner keeps (a Planner's plan and task journal, the
Manager's recent chats). Without a running old session, the brief is built
from that state alone.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING

from .runtimes import CLAUDE_CODE, can_resume

if TYPE_CHECKING:
    from .agent_settings import Launch


class SwitchReason(str, Enum):
    AGENT_CHANGED = "agent_changed"
    SESSION_LOST = "session_lost"


@dataclass(frozen=True)
class SessionSwitch:
    """A resident agent's move to a new session: why, and from which runtime and agent to which."""

    reason: SwitchReason
    detail: str  # why, in words: what cannot resume what, or what happened to the session
    old_runtime: str | None = None
    old_agent: str | None = None
    new_runtime: str | None = None
    new_agent: str | None = None

    def describe(self) -> str:
        """One line for the task journal and the activity."""
        def side(runtime: str | None, agent: str | None) -> str:
            return f"{runtime or '?'}" + (f" (agent {agent})" if agent else "")

        if self.reason is SwitchReason.AGENT_CHANGED:
            return (f"agent changed from {side(self.old_runtime, self.old_agent)} to "
                    f"{side(self.new_runtime, self.new_agent)}: {self.detail}")
        return f"session lost: {self.detail}"


def agent_switch(
    session_id: str | None, session_runtime: str | None, session_agent: str | None, launch: Launch,
) -> SessionSwitch | None:
    """The switch a turn about to run with ``launch`` makes from the session saved with ``session_runtime`` and
    ``session_agent`` (None: from before agents, which Claude Code ran); None when there is no saved session
    or the turn's agent resumes it."""
    if not session_id or can_resume(session_runtime, launch.runtime):
        return None
    old = session_runtime or CLAUDE_CODE.type
    return SessionSwitch(
        SwitchReason.AGENT_CHANGED,
        f"a {launch.runtime.type} process cannot resume a {old} session",
        old_runtime=old, old_agent=session_agent, new_runtime=launch.runtime.type, new_agent=launch.agent.id,
    )


def session_lost(detail: str, *, runtime: str | None = None, agent: str | None = None) -> SessionSwitch:
    """The switch to a new session when the saved one cannot be resumed, or there is none."""
    return SessionSwitch(SwitchReason.SESSION_LOST, detail, old_runtime=runtime, old_agent=agent,
                         new_runtime=runtime, new_agent=agent)
