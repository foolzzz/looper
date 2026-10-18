"""System prompt policy, shared without importing runtime adapter discovery."""

from enum import Enum


class SystemPromptMode(str, Enum):
    """How Looper's prompt relates to the runtime's default system prompt."""

    APPEND = "append"
    REPLACE = "replace"
