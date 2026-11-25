"""Whole-message commands a task conversation answers without a model."""

from __future__ import annotations

from enum import Enum


class TaskCommand(str, Enum):
    STOP = "/stop"
    DONE = "/done"
    PAUSE = "/pause"
    BACKGROUND = "/background"
    TAKEOVER = "/takeover"
    START = "/start"


def parse(text: str) -> TaskCommand | None:
    """The whole message, trimmed and case-folded. ``/stop now`` is not a command."""
    folded = text.strip().casefold()
    for command in TaskCommand:
        if command.value.casefold() == folded:
            return command
    return None
