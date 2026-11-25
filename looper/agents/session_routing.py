"""Which Manager session a conversation runs in, and what that means for a group chat's turns.

By default every conversation of a Manager shares ``main`` (``tiers.manager_session``): a group chat's turn runs
in the context of the operator's own conversation. With ``manager.session_routing.enabled`` on,
the default Manager gives each Lark or Telegram group and each other allowed sender's direct chat a session of its
own (``session_key_for``), run in parallel with the others, at most ``manager.sessions.max_resident`` of them
resident at once. Scenario Managers opt in independently with ``session_routing=per_chat`` in their definition;
only their groups are routed, and their other turns keep main.

A feature that must not reach a group through the shared context (what the operator said elsewhere, the operator's
notes) checks ``group_isolation`` and stays off in group turns until it reports ``GroupIsolation.ISOLATED``.

The global settings are kept in memory: ``load`` reads them at startup (from ``chat_settings.load``) and an update
applies them at once. Scenario definitions are applied at startup and by the fleet's change listener.
Until loaded, routing is off.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
from typing import Any

from .message import is_lark
from .operator_identity import is_operator

ROUTING_KEY = "manager.session_routing.enabled"
DEFAULT_ROUTING_ENABLED = False
MAX_RESIDENT_KEY = "manager.sessions.max_resident"
DEFAULT_MAX_RESIDENT = 2  # RFC 4.2: a resident session costs about 240-310 MB; resuming on demand is 1-2 s slower
MAX_RESIDENT_RANGE = (1, 16)

_routing_enabled = DEFAULT_ROUTING_ENABLED
_max_resident = DEFAULT_MAX_RESIDENT
_scenario_routing: dict[str, str] = {}


class GroupIsolation(str, Enum):
    """How far a group chat's (or another sender's direct chat's) turns are kept apart from the operator's own
    conversation."""

    SHARED = "shared"  # they run in the operator's session, with everything it holds
    ISOLATED = "isolated"  # they run in a session of their own that holds only their own chat


class SessionKind(str, Enum):
    MAIN = "main"  # the dashboard chat, API clients and the operator's own direct chats
    GROUP = "group"  # one Lark or Telegram group chat (its topics included)
    DM = "dm"  # the direct chat of another allowed sender


@dataclass(frozen=True)
class SessionKey:
    """A Manager session's key: ``main``, or a group's or another sender's direct chat on one bot."""

    kind: SessionKind
    channel_id: str | None = None
    chat: str | None = None

    def __post_init__(self) -> None:
        if (self.kind is SessionKind.MAIN) != (self.channel_id is None and self.chat is None) \
                or (self.kind is not SessionKind.MAIN and not (self.channel_id and self.chat)):
            raise ValueError(f"a {self.kind.value} session key needs {'no' if self.kind is SessionKind.MAIN else 'a'}"
                             " channel and chat")

    @property
    def text(self) -> str:
        return SessionKind.MAIN.value if self.kind is SessionKind.MAIN else f"{self.kind.value}:{self.channel_id}:{self.chat}"


MAIN_KEY = SessionKey(SessionKind.MAIN)


def session_key_for(origin: Mapping[str, Any], *, client_id: str | None, operator_id: str | None) -> SessionKey:
    """The session a message from ``origin`` (``tiers.manager._message_origin``: its channel, bot, chat, sender and
    whether the chat is a group) runs in. The dashboard chat (with or without an API client) and the operator's own
    direct chats are ``main``; so is any direct chat of a bot with no ``operator_id``, which cannot tell the operator
    apart (RFC 5.3.1). A group is ``group``; another sender's direct chat is ``dm``."""
    channel, channel_id, chat = origin.get("channel"), origin.get("channel_id"), origin.get("chat")
    if not (is_lark(channel) or channel == "telegram") or not channel_id or not chat:
        return MAIN_KEY
    if origin.get("group"):
        return SessionKey(SessionKind.GROUP, channel_id, chat)
    if operator_id is None or is_operator(channel=channel, group=False, sender=origin.get("sender"),
                                          client_id=client_id, operator_id=operator_id):
        return MAIN_KEY
    return SessionKey(SessionKind.DM, channel_id, chat)


def resident_label(manager_id: str, key: SessionKey) -> str:
    """The label of a session's resident runtime (its logs, its session directory): the Manager's id for ``main``,
    as before routing; the Manager's id, the kind and a digest of the key for the others."""
    if key.kind is SessionKind.MAIN:
        return manager_id
    return f"{manager_id}.{key.kind.value}.{key_digest(key.text)}"


def key_digest(text: str) -> str:
    """A session key's short digest, for the names of what each session keeps apart (its binding file, its label)."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]


def session_digest(manager_id: str, key: str) -> str:
    """Keep the default Manager's paths stable; other Managers must not share its runtime files."""
    from .managers_api import DEFAULT_MANAGER_ID

    return key_digest(key if manager_id == DEFAULT_MANAGER_ID else f"{manager_id}:{key}")


def routing_enabled() -> bool:
    return _routing_enabled


def set_routing_enabled(value: bool) -> None:
    """Apply a value in memory only (``load`` and ``chat_settings.update`` store it; tests use this directly)."""
    global _routing_enabled
    _routing_enabled = value


def max_resident() -> int:
    return _max_resident


def set_max_resident(value: int) -> None:
    """Apply a value in memory only (``load`` and ``chat_settings.update`` store it; tests use this directly)."""
    global _max_resident
    _max_resident = checked_max_resident(value)


def checked_max_resident(value: Any) -> int:
    low, high = MAX_RESIDENT_RANGE
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise ValueError(f"max_resident must be an integer from {low} to {high}")
    return value


def routes(manager_id: str) -> bool:
    """Default Manager: global switch. Scenario Manager: its independent typed definition setting."""
    from .managers_api import DEFAULT_MANAGER_ID  # here: the Managers' definitions import the chat settings

    return _routing_enabled if manager_id == DEFAULT_MANAGER_ID else _scenario_routing.get(manager_id) == "per_chat"


def set_scenario_routing(manager_id: str, value: str) -> None:
    """Apply a definition at startup or after the fleet reads a committed change."""
    from .managers_api import DEFAULT_MANAGER_ID, ScenarioSessionRouting

    mode = ScenarioSessionRouting(value)
    if manager_id != DEFAULT_MANAGER_ID:
        if mode is ScenarioSessionRouting.OFF:
            _scenario_routing.pop(manager_id, None)
        else:
            _scenario_routing[manager_id] = mode.value


def apply_stored(stored: Mapping[str, str]) -> None:
    """Apply the stored settings (``system_settings`` rows); a missing or bad value is the default."""
    set_routing_enabled(stored.get(ROUTING_KEY) == "true")
    try:
        set_max_resident(int(stored.get(MAX_RESIDENT_KEY, DEFAULT_MAX_RESIDENT)))
    except ValueError:
        set_max_resident(DEFAULT_MAX_RESIDENT)


def group_isolation(manager_id: str) -> GroupIsolation:
    """How the group turns of Manager ``manager_id`` are kept apart now: while it routes, in sessions of their own
    that start with only their own chat (``tiers.group_session``: ``ISOLATED``), else ``SHARED``."""
    return GroupIsolation.ISOLATED if routes(manager_id) else GroupIsolation.SHARED
