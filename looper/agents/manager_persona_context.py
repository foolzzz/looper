"""Compile the persona for this message's audience, independently of session routing.

Shared group/other-sender turns omit it; their own sessions get only the name.
The dashboard, API clients and the operator's direct chats keep the full persona.
"""

import logging
from collections.abc import Sequence
from dataclasses import replace
from enum import Enum

from . import operator_identity, persona
from .managers_api import DEFAULT_MANAGER_ID
from .message import BOT_CHANNEL_TYPES, MessageEnvelope
from .session_routing import SessionKey, SessionKind

logger = logging.getLogger(__name__)
HEADING = "Persona (set by the operator: your name and tone; Looper's rules still apply):"
NAME_HEADING = "Persona (set by the operator: your name; Looper's rules still apply):"


class Scope(Enum):
    FULL = "full"
    NAME = "name"
    OMIT = "omit"


async def scope(manager_id: str, route: SessionKey, envelope: MessageEnvelope | None, *,
                shown: Sequence[str] = (), private: bool = False) -> Scope:
    """Use trusted message origin; an unknown operator never grants access to private fields."""
    from .tiers.manager import _message_origin

    value = persona.current()
    # Clearing the live value can still leave a private persona in main's last whole block.
    had_persona = bool(shown and shown[0] == HEADING)
    if manager_id != DEFAULT_MANAGER_ID or not (value.name or value.tone or value.notes or had_persona or private):
        return Scope.FULL  # off: no identity read or change to the stable block
    if route.kind is not SessionKind.MAIN:
        return Scope.NAME
    if envelope is None:
        return Scope.FULL
    origin = _message_origin(envelope)
    if origin["channel"] not in BOT_CHANNEL_TYPES:
        return Scope.FULL
    if not origin["group"]:
        try:
            operator_id = await operator_identity.operator_sender_id(origin["channel_id"])
        except Exception:
            logger.warning("could not read persona audience identity; omitting persona", exc_info=True)
            operator_id = None
        if operator_identity.is_operator(channel=origin["channel"], group=False, sender=origin["sender"],
                                         client_id=None, operator_id=operator_id):
            return Scope.FULL
    return Scope.OMIT


def section(manager_id: str, audience: Scope) -> list[str]:
    """Read the validated live value; leave scenario Managers' sections unchanged."""
    if manager_id != DEFAULT_MANAGER_ID or audience is Scope.OMIT:
        return []
    # Keep the stored address compatible with existing settings, without exposing it to the model.
    value = replace(persona.current(), address="")
    lines = persona.section(value, group=audience is Scope.NAME)
    heading = NAME_HEADING if audience is Scope.NAME else HEADING
    return [heading, *lines[1:]] if lines else []
