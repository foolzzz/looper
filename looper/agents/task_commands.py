"""Whole-message commands a task conversation answers without a model."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from enum import Enum
from typing import Any

from ..db.tasks import InteractionMode, interaction_mode


class TaskCommand(str, Enum):
    STOP = "/stop"
    DONE = "/done"
    PAUSE = "/pause"
    BACKGROUND = "/background"
    TAKEOVER = "/takeover"
    START = "/start"


# Stored on chat_messages.entry_kind. Model history reads this column; it does not parse the text again.
ENTRY_MESSAGE = "message"
ENTRY_COMMAND = "command"
ENTRY_COMMAND_REPLY = "command_reply"
_ENTRY_KINDS = frozenset({ENTRY_MESSAGE, ENTRY_COMMAND, ENTRY_COMMAND_REPLY})


def parse(text: str) -> TaskCommand | None:
    """The whole message, trimmed and case-folded. ``/stop now`` is not a command."""
    folded = text.strip().casefold()
    for command in TaskCommand:
        if command.value.casefold() == folded:
            return command
    return None


# A task message keeps this text when the same id is sent again with another body.
MESSAGE_TEXT_CONFLICT = "this message_id was already sent with different text"
# Code replies. They are not model output.
STARTING_REPLY = "已存储，启动中"
ORCHESTRATED_REPLY = "这句话已留下，没有交给任务管家。编排中的纠正还不能从这里送出。"

# The background is starting. The message is kept; a task Manager turn would race that start.
_STARTING_STATUSES = frozenset({"pending", "assigned"})
DIRECT = "direct"
STARTING = "starting"
ORCHESTRATED = "orchestrated"


def delivery_route(status: str | None) -> str:
    """How an ordinary task message is answered from the task's status.

    ``pending`` and ``assigned`` are stored and answered in code: the background is starting.
    Every other active status is orchestrated. Quoting the message into a planner supplement is a later step,
    so the message is kept and refused instead of being handed to the task Manager. Anything else, including a
    status this mapping does not know, is direct.
    """
    if status in _STARTING_STATUSES:
        return STARTING
    if interaction_mode(status) is InteractionMode.ORCHESTRATED:
        return ORCHESTRATED
    return DIRECT


def _command_item(item: Any) -> bool:
    if isinstance(item, str):
        return parse(item) is not None
    if isinstance(item, Mapping) and item.get("role") == "user":
        content = item.get("content")
        return isinstance(content, str) and parse(content) is not None
    return False


def _entry_kind(item: Any) -> str | None:
    if not isinstance(item, Mapping):
        return None
    kind = item.get("entry_kind")
    return kind if kind in _ENTRY_KINDS else None


def history_for_model(history: Sequence[Any]) -> list[Any]:
    """Drop fixed commands and the code-written reply that follows each one.

    A row with ``entry_kind`` ``command`` or ``command_reply`` is out, whatever its text says. A row marked
    ``message`` stays, even when the text is ``/done``. A row with no kind still uses the whole trimmed message:
    the rows after it that are not a later user message (its code reply, and a tool or event row between them)
    stay out of every model path, including a fresh retry after resume fails.
    """
    kept: list[Any] = []
    skip_reply = False
    for item in history:
        kind = _entry_kind(item)
        if kind == ENTRY_COMMAND:
            skip_reply = True
            continue
        if kind == ENTRY_COMMAND_REPLY:
            continue
        if kind == ENTRY_MESSAGE:
            skip_reply = False
            kept.append(item)
            continue
        if skip_reply:
            role = item.get("role") if isinstance(item, Mapping) else None
            if role == "assistant":
                skip_reply = False
                continue
            if role in ("tool", "event"):
                continue
            skip_reply = False
        if _command_item(item):
            skip_reply = True
            continue
        kept.append(item)
    return kept
