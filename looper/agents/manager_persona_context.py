"""Compile the persona for this message's audience, independently of session routing.

Shared group/other-sender turns omit it; their own sessions get only the name.
The dashboard, API clients and the operator's direct chats keep the full persona.
With a private profile, API clients omit it and keep main's stable baseline isolated.
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
    PERSONA_ONLY = "persona_only"  # full persona, without the private operator profile
    NAME = "name"
    OMIT = "omit"

    @property
    def isolated(self) -> bool:
        """Render without main's private baseline and do not replace its last shown state."""
        return self in (Scope.OMIT, Scope.PERSONA_ONLY)


async def _is_operator(envelope: MessageEnvelope) -> bool:
    """Check the trusted origin, including an API client's verified identity; failures grant nothing."""
    from .tiers.manager import _message_client, _message_origin

    origin = _message_origin(envelope)
    operator_id = None
    if origin["channel"] in BOT_CHANNEL_TYPES and not origin["group"]:
        try:
            operator_id = await operator_identity.operator_sender_id(origin["channel_id"])
        except Exception:
            logger.warning("could not read persona audience identity; omitting persona", exc_info=True)
            return False
    return operator_identity.is_operator(
        channel=origin["channel"], group=origin["group"], sender=origin["sender"],
        client_id=_message_client(envelope)[0], operator_id=operator_id)


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
        if private and not await _is_operator(envelope):
            return Scope.PERSONA_ONLY
        return Scope.FULL
    if await _is_operator(envelope):
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
