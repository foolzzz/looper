"""One Manager conversation's state (``ManagerSession``), apart from the Manager that runs it.

``ManagerAgent`` keeps its sessions by key, only ``MAIN_SESSION`` for now. Its turn code reads and writes the
session it runs on through properties under the attributes' old names (``session_field``): the session a context
sets (``ManagerAgent._current``), else main.
"""

from __future__ import annotations

import asyncio
import logging
import weakref
from collections.abc import Callable
from dataclasses import dataclass, field, fields
from typing import TYPE_CHECKING, Any

from ..run_usage import Usage
from ..session_routing import MAIN_KEY, SessionKey

if TYPE_CHECKING:
    from datetime import datetime

    from ..agent_settings import AgentSettings, Launch
    from ..research_order import ResearchSources
    from ..runtimes import ResidentSession, RuntimeAdapter
    from ..session_switch import SessionSwitch
    from ..system_prompt import SystemPrompt
    from .state_view import StateShown
    from .turn_token import TurnToken
    from .manager_budget import BudgetTurn

logger = logging.getLogger(__name__)

MAIN_SESSION = "main"


_REPLACED: list[weakref.WeakMethod[Callable[[str], None]]] = []


def on_session_replaced(listener: Callable[[str], None]) -> None:
    """Call ``listener(key)`` whenever a Manager session's saved session is cleared or replaced: /new, the dashboard
    reset, a switch of agent, a rebuild after a failed resume. Held weakly. The key is the session's, not its
    Manager's, so a listener drops what it kept for that key, whichever Manager's session it was."""
    _REPLACED.append(weakref.WeakMethod(listener))  # type: ignore[arg-type]


def _session_replaced(key: str) -> None:
    for ref in list(_REPLACED):
        listener = ref()
        if listener is None:
            _REPLACED.remove(ref)
            continue
        try:
            listener(key)
        except Exception:  # a listener must never abort the reset that called it
            logger.exception("a listener of the reset of Manager session %s failed", key)


@dataclass(frozen=True)
class _Process:
    """What the resident Claude was started with; a turn on it is recorded with these."""

    command: list[str]
    signature: Any
    system_prompt: SystemPrompt
    launch: Launch
    signature_components: dict[str, str] = field(default_factory=dict)


@dataclass(eq=False, kw_only=True)
class ManagerSession:
    """Everything that belongs to one Manager conversation: its resident runtime, its saved session, its turn lock
    and the turn holding it, and what the next turn is told."""

    key: str
    route: SessionKey = MAIN_KEY  # the conversation it holds (``session_routing.session_key_for``); key is its text
    # The resident Claude: started on the first chat message, then fed one
    # user turn per message; restarted when its system prompt changes.
    claude: ResidentSession
    claude_runtime: RuntimeAdapter  # the runtime type whose session claude is
    session_id: str | None = None
    # The session's running totals (cost and tokens) at its last result. A process that resumes the
    # session starts from them, so its first turn records only its own share.
    session_totals: Usage = field(default_factory=Usage)
    # The runtime type and the provider the session belongs to and the Agent Profile that ran it last (None:
    # a session saved before they were recorded).
    session_runtime: str | None = None
    session_profile: str | None = None
    session_provider: str | None = None
    turn_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    # Serializes the resident's admission, reset and idle close. An idle check never waits for a busy turn.
    lifecycle_gate: asyncio.Lock = field(default_factory=asyncio.Lock)
    process: _Process | None = None  # what the running Claude was started with
    # Why the session was last dropped and the handoff its old session wrote, until the new session's first
    # turn has its resume brief.
    pending_switch: SessionSwitch | None = None
    pending_handoff: str | None = None
    # The user dropped the session (/new): the next one starts without a resume brief. Saved with the session,
    # so a restart before the next message keeps it; cleared once the new session has started.
    clean_start: bool = False
    session_loaded: bool = False  # whether the saved session was read (at start, else before the first turn)
    # The last whole "Looper state" block the session got (``state_view``); in memory only, so the first turn
    # after a restart sends it whole. None: none a turn may refer to, for ``state_lost``'s reason.
    state_shown: StateShown | None = None
    state_lost: str = "no whole block yet"
    # Moves on when the session's context may have lost what earlier turns showed: a compaction it reported, a
    # turn it runs on its own (whose stream is not read for one). A turn rendered before it moved starts whole
    # and leaves no block behind.
    state_generation: int = 0
    # Told to the next user turn: the user stopped or withdrew the ones before.
    stopped_notes: list[str] = field(default_factory=list)
    lock_owner: TurnToken | None = None  # the turn that holds ``turn_lock`` from its preparation
    # When this session's last user turn, and its last background turn, released the lock.
    # Written only while metrics are on; read by queued_behind. Not persisted.
    last_user_turn_ended_at: datetime | None = None
    last_background_turn_ended_at: datetime | None = None
    running_token: TurnToken | None = None  # the token of the user turn being answered
    research: ResearchSources | None = None  # where the turn being prepared may look things up, when on
    # The agent settings the last turn ran with, kept when a turn cannot read them (None before any).
    agent_settings: AgentSettings | None = None
    last_activity: float
    turn_ended_at: float | None = None  # this session's own worker/loop end, for main's idle-check clock
    budget: BudgetTurn | None = None  # enabled turn only; retained through reply and session maintenance

    def __setattr__(self, name: str, value: Any) -> None:
        # The one primitive every reset path goes through: a saved session cleared or replaced by another.
        old = self.__dict__.get("session_id") if name == "session_id" else None
        object.__setattr__(self, name, value)
        if old is not None and value != old:
            _session_replaced(self.key)


_FIELDS = frozenset(f.name for f in fields(ManagerSession))


def session_field(name: str) -> Any:
    """A ``ManagerAgent`` property under the old name of one of its session attributes: it reads and writes field
    ``name`` of the session the code runs on (``ManagerAgent._session_now``)."""
    if name not in _FIELDS:
        raise AttributeError(f"ManagerSession has no field {name!r}")

    def get(agent: Any) -> Any:
        return getattr(agent._session_now(), name)

    def set_(agent: Any, value: Any) -> None:
        setattr(agent._session_now(), name, value)

    return property(get, set_, doc=f"``ManagerSession.{name}`` of the session the code runs on.")
