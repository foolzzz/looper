"""Whole-message commands a task conversation answers without a model."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from enum import Enum
from typing import Any

from ..db.tasks import InteractionMode, TaskGroup, interaction_mode, task_group


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

# Answered from the live turn or the task status, not from NOT_YET_REPLIES.
_LIVE_REPLIES = frozenset({TaskCommand.STOP, TaskCommand.DONE, TaskCommand.START})


def _require_reply_for_every_command(replies: Mapping[TaskCommand, str]) -> Mapping[TaskCommand, str]:
    """Each member is live or named once. A new command must not reuse another command's sentence."""
    missing = [command.value for command in TaskCommand if command not in replies and command not in _LIVE_REPLIES]
    overlap = [command.value for command in replies if command in _LIVE_REPLIES]
    if missing or overlap:
        raise RuntimeError(
            f"task command replies must cover each command once; missing={missing} overlap={overlap}")
    return replies


NOT_YET_REPLIES: Mapping[TaskCommand, str] = _require_reply_for_every_command({
    TaskCommand.BACKGROUND: "这个命令要等「交给后台」上线后才能用",
    TaskCommand.PAUSE: "此控制将在后续步骤提供",
    TaskCommand.TAKEOVER: "此控制将在后续步骤提供",
})


# ``/start`` names the control the dashboard actually shows for that group (``db.tasks.task_group``).
# Ready and draft: the compact 开始 control sits above the draft. In progress and needs-you: the
# planner is running or starting (``ACTIVE_STATUSES``) and that control is absent. Attention and
# cancelled: 继续后台任务 is in the 任务详情 panel. Completed has no start control.
START_UNSTARTED = "请用上方的「开始」按钮启动任务"
START_IN_PROGRESS = "任务已经在进行中"
START_RESUME = "请用「任务详情」里的「继续后台任务」按钮继续"
START_COMPLETED = "任务已经完成"
START_FALLBACK = "请用任务控制里的按钮操作"
_START_BY_GROUP: Mapping[TaskGroup, str] = {
    TaskGroup.READY: START_UNSTARTED,
    TaskGroup.DRAFT: START_UNSTARTED,
    TaskGroup.IN_PROGRESS: START_IN_PROGRESS,
    TaskGroup.NEEDS_YOU: START_IN_PROGRESS,
    TaskGroup.ATTENTION: START_RESUME,
    TaskGroup.CANCELLED: START_RESUME,
    TaskGroup.COMPLETED: START_COMPLETED,
}


def start_reply(status: str | None) -> str:
    """The ``/start`` sentence for this status. An unknown status is the neutral fallback.

    The caller logs the fallback. This does not start the task.
    """
    if not isinstance(status, str):
        return START_FALLBACK
    return _START_BY_GROUP.get(task_group(status), START_FALLBACK)


def fixed_command_reply(command: TaskCommand) -> str:
    """The code reply for a command this conversation does not run.

    ``/stop``, ``/done`` and ``/start`` are answered from the live turn and the task status. Any other member
    must be in ``NOT_YET_REPLIES``; a missing one raises instead of borrowing another command's sentence.
    """
    try:
        return NOT_YET_REPLIES[command]
    except KeyError:
        raise RuntimeError(f"no reply for task command {command!r}") from None


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
