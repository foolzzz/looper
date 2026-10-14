"""Why a resident agent starts a new session instead of resuming its saved one (multi-runtime RFC P5).

The Manager and a task's Planner each keep one session and resume it after a
restart. Their next turn starts a new session instead when:

- ``PROFILE_CHANGED``: the Agent Profile the turn runs cannot resume the saved
  session. ``profile_switch`` decides it before every turn, from what the
  session was saved with and the profile the turn resolved. It is the one place the rule
  lives (``runtimes.can_resume``): a session resumes only when both its
  runtime type and its provider are unchanged, so another runtime type, or any
  change of provider (subscription to API key, API key to subscription, one API
  key provider to another), starts a new session; a change of model alone does
  not.
- ``SESSION_LOST``: the runtime could not resume the saved session (it no
  longer exists), or there is none to resume for work under way.

Before a ``PROFILE_CHANGED`` switch, an old session whose process still runs
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

from .runtimes import CLAUDE_CODE, LEGACY_SESSION_PROVIDER_ID, can_resume

if TYPE_CHECKING:
    from .agent_settings import Launch


class SwitchReason(str, Enum):
    PROFILE_CHANGED = "profile_changed"
    SESSION_LOST = "session_lost"


@dataclass(frozen=True)
class SessionSwitch:
    """A resident agent's move to a new session: why, and from which runtime and Agent Profile to which."""

    reason: SwitchReason
    detail: str  # why, in words: what cannot resume what, or what happened to the session
    old_runtime: str | None = None
    old_profile: str | None = None
    new_runtime: str | None = None
    new_profile: str | None = None
    old_provider: str | None = None
    new_provider: str | None = None

    def describe(self) -> str:
        """One line for the task journal and the activity. Agents read it in their handoff and resume brief, so
        its words stay as they were before Agent Profiles were named."""
        def side(runtime: str | None, profile: str | None) -> str:
            return f"{runtime or '?'}" + (f" (agent {profile})" if profile else "")

        if self.reason is SwitchReason.PROFILE_CHANGED:
            return (f"agent changed from {side(self.old_runtime, self.old_profile)} to "
                    f"{side(self.new_runtime, self.new_profile)}: {self.detail}")
        return f"session lost: {self.detail}"


def profile_switch(
    session_id: str | None, session_runtime: str | None, session_profile: str | None, launch: Launch, *,
    session_provider: str | None,
) -> SessionSwitch | None:
    """The switch a turn about to run with ``launch`` makes from the session saved with ``session_runtime``,
    ``session_provider`` and ``session_profile`` (None runtime: from before Agent Profiles, which Claude Code ran;
    None provider: from before sessions recorded it, Claude Code's own login); None when there is no saved session
    or the turn's profile resumes it."""
    provider = launch.profile.provider.id
    if not session_id or can_resume(session_runtime, session_provider, launch.runtime, provider):
        return None
    old, old_provider = session_runtime or CLAUDE_CODE.type, session_provider or LEGACY_SESSION_PROVIDER_ID
    detail = (f"a {launch.runtime.type} process cannot resume a {old} session" if old != launch.runtime.type
              else f"a session of provider {old_provider} cannot go on with provider {provider}")
    return SessionSwitch(
        SwitchReason.PROFILE_CHANGED, detail,
        old_runtime=old, old_profile=session_profile, new_runtime=launch.runtime.type, new_profile=launch.profile.id,
        old_provider=old_provider, new_provider=provider,
    )


def session_lost(detail: str, *, runtime: str | None = None, profile: str | None = None) -> SessionSwitch:
    """The switch to a new session when the saved one cannot be resumed, or there is none."""
    return SessionSwitch(SwitchReason.SESSION_LOST, detail, old_runtime=runtime, old_profile=profile,
                         new_runtime=runtime, new_profile=profile)
