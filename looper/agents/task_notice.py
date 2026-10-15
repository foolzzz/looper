"""Telling the chat a task came from that the task ended.

Telegram and Lark can send a message unprompted (``ChannelAdapter.notify``),
so a task asked for in one of them is told of its end there: when it
completes or fails, the TaskRouter sends one notice, in the background, to
the chat the task recorded when the Manager created it (``tasks.chat_id``,
in the form that channel's ``notify`` takes). The Manager forwards a task's
progress to the chat it came from, but not its end to these channels, so the
end arrives once. The web chat cannot be sent to unprompted: it still gets
the task's final status from the Manager, and the dashboard shows the task.
"""

from __future__ import annotations

from enum import Enum

from ..db import tasks as db_tasks
from .message import channel_address
from .schedule_notify import snippet

NAME_CHARS = 40


class NotifyChannel(str, Enum):
    """A channel whose adapter implements ``ChannelAdapter.notify``, and so is told when a task asked for in it
    ends."""

    TELEGRAM = "telegram"
    LARK = "lark"


_BY_CHANNEL_ID = {channel_address(channel.value).agent_id: channel for channel in NotifyChannel}


def notify_channel(origin_channel_id: str | None) -> NotifyChannel | None:
    """The channel a task's ``origin_channel_id`` names, when it is one that is told of the task's end."""
    return _BY_CHANNEL_ID.get(origin_channel_id or "")


def notify_chat(origin_channel_id: str | None, *, sender: str | None, chat: str | None) -> str | None:
    """Who is told of the end of a task asked for by ``sender`` in ``chat``, in the form the channel's ``notify``
    takes: Lark's sends to a user (the sender's open_id), Telegram's to a chat (its id). None for a channel that
    cannot be told."""
    match notify_channel(origin_channel_id):
        case NotifyChannel.LARK:
            return sender or None
        case NotifyChannel.TELEGRAM:
            return chat or None
    return None


def ends_task(status: str | None) -> bool:
    """Whether a Planner's status is its task's end: completed, or failed (``db.tasks.PLANNER_END_STATUSES``)."""
    return isinstance(status, str) and status in db_tasks.PLANNER_END_STATUSES


def notice_text(task_id: str, requirement: str, status: str, summary: str | None) -> str:
    """What ended and how, the start of its summary (or closing note), and the link to the task."""
    flat = " ".join(requirement.split())
    name = (flat if len(flat) <= NAME_CHARS else f"{flat[:NAME_CHARS]}…") or task_id
    head = "✅ 任务完成" if db_tasks.task_group(status) is db_tasks.TaskGroup.COMPLETED else "❌ 任务失败"
    lines = [f"{head} · {name}"]
    if text := snippet(summary):
        lines.append(text)
    lines.append(f"/#tasks/{task_id}")
    return "\n".join(lines)
