"""Which Manager session a conversation runs in, and what that means for a group chat's turns.

Today every conversation of a Manager shares one session, ``main`` (``tiers.manager_session``): a group chat's turn
runs in the context of the operator's own conversation. A feature that must not reach a group through that shared
context (what the operator said elsewhere, the operator's notes) checks ``group_isolation`` and stays off in group
turns until it reports ``GroupIsolation.ISOLATED``.
"""

from __future__ import annotations

from enum import Enum


class GroupIsolation(str, Enum):
    """How far a group chat's (or another sender's direct chat's) turns are kept apart from the operator's own
    conversation."""

    SHARED = "shared"  # they run in the operator's session, with everything it holds
    ROUTED = "routed"  # they run in a session of their own, which can still start with other chats' messages
    ISOLATED = "isolated"  # they run in a session of their own that holds only their own chat


def group_isolation(manager_id: str) -> GroupIsolation:
    """How the group turns of Manager ``manager_id`` are kept apart now: always ``SHARED`` while each Manager has only
    its ``main`` session."""
    return GroupIsolation.SHARED
