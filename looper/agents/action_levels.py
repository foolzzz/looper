"""Where a call's effect reaches (the outbound-review RFC, section 4.1): the typed level every tool and operation
declares, and the level of a push target. Only ``external`` and ``unknown`` calls are reviewed (``outbound``).

It imports none of Looper's modules at load (only ``target_level`` does, when it runs), so the tool and operation
declarations can use it."""

from __future__ import annotations

from collections.abc import Mapping
from enum import Enum
from typing import Any


class ActionLevel(str, Enum):
    """Where a call's effect reaches; only ``external`` and ``unknown`` are reviewed."""

    INSIDE = "inside"  # Looper's own state: tasks, plans, settings, files under the runtime root
    OPERATOR = "operator"  # only the operator: their direct chat, the dashboard
    ORIGIN = "origin"  # only the chat the work came from: an answer, a gate's discussion, a task notice
    MACHINE = "machine"  # runs commands or changes files and configuration on this machine, outside the runtime root
    EXTERNAL = "external"  # leaves this machine for someone or something other than the operator
    UNKNOWN = "unknown"  # a runtime tool with no type (a shell): a call may be any of the above


REVIEWED = frozenset({ActionLevel.EXTERNAL, ActionLevel.UNKNOWN})
# From the least to the most reviewed: a call made of several effects is at its highest one's level.
_ORDER = (ActionLevel.INSIDE, ActionLevel.OPERATOR, ActionLevel.ORIGIN, ActionLevel.MACHINE, ActionLevel.EXTERNAL,
          ActionLevel.UNKNOWN)


def highest(*levels: ActionLevel) -> ActionLevel:
    """The level of a call that has each of ``levels``' effects."""
    return max(levels, key=_ORDER.index, default=ActionLevel.INSIDE)


async def target_level(target: Any, *, channel: str | None = None, chat: str | None = None,
                       sender: str | None = None) -> ActionLevel:
    """The level of pushing to ``target`` (``{channel, chat}``; None: the dashboard only): ``operator`` for the
    dashboard or the operator's own direct chat (``operator_identity``), ``origin`` for the chat of the turn that asks
    (``channel``, ``chat`` and, in a direct chat, its ``sender``), else ``external``; one that names no chat is
    ``unknown``."""
    from .message import builtin_channel_id
    from .operator_identity import operator_sender_id

    if target is None:
        return ActionLevel.OPERATOR
    if not isinstance(target, Mapping) or not isinstance(target.get("channel"), str) \
            or not isinstance(target.get("chat"), str) or not target["chat"]:
        return ActionLevel.UNKNOWN
    to_channel, to_chat = target["channel"], target["chat"]
    try:
        operator = await operator_sender_id(builtin_channel_id(to_channel))
    except Exception:
        operator = None
    if operator is not None and to_chat == operator:
        return ActionLevel.OPERATOR
    if channel is not None and to_channel == channel and to_chat in {c for c in (chat, sender) if c}:
        return ActionLevel.ORIGIN
    return ActionLevel.EXTERNAL
