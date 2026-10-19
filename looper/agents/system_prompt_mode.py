"""System prompt policy, shared without importing runtime adapter discovery."""

from enum import Enum


class SystemPromptMode(str, Enum):
    """How Looper's prompt relates to the runtime's default system prompt."""

    APPEND = "append"
    REPLACE = "replace"


def system_prompt_mode(raw: object) -> SystemPromptMode:
    """A role's mode as stored or given: ``append`` or ``replace``."""
    try:
        return SystemPromptMode(raw)
    except ValueError:
        raise ValueError(f'system_prompt_mode must be "append" or "replace"; got {raw!r}') from None
