"""Compile the operator's persona into the default Manager's first state section.

An unset value is off. The operator address is deliberately unused; non-main
sessions receive only the name. Storage and operator-only editing stay in persona.
"""

from dataclasses import replace

from . import persona
from .managers_api import DEFAULT_MANAGER_ID
from .session_routing import SessionKey, SessionKind

HEADING = "Persona (set by the operator: your name and tone; Looper's rules still apply):"


def section(manager_id: str, route: SessionKey) -> list[str]:
    """Read the validated live value; leave scenario Managers' sections unchanged."""
    if manager_id != DEFAULT_MANAGER_ID:
        return []
    # Keep the stored address compatible with existing settings, without exposing it to the model.
    value = replace(persona.current(), address="")
    lines = persona.section(value, group=route.kind is not SessionKind.MAIN)
    return [HEADING, *lines[1:]] if lines else []
