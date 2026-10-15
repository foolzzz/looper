"""Telling the chat a task came from that the task ended.

Telegram and Lark can send a message unprompted (``ChannelAdapter.notify``),
so a task asked for in one of them is told of its end there, once: when it
completes or fails, the TaskRouter adds a ``Notice`` to the final status it
passes to the Manager, for who the task recorded when the Manager created it
(``tasks.chat_id``, in the form that channel's ``notify`` takes). The Manager
sends the notice to the channel instead of the final summary, on the same
path as the task's progress, so it is the task's last message in that chat.
A task without a notice (the web chat, or one that recorded no chat) gets
its final status from the Manager as before; the dashboard shows the task.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
from typing import Any

from ..db import tasks as db_tasks
from .links import task_link
from .message import channel_address

NAME_CHARS = 40
LEAD_CHARS = 300
# The start of the second section of a Markdown summary: a heading line after the first line.
_NEXT_SECTION = re.compile(r"\n(?=#{1,6} )")


class NotifyChannel(str, Enum):
    """A channel whose adapter implements ``ChannelAdapter.notify``, and so is told when a task asked for in it
    ends."""

    TELEGRAM = "telegram"
    LARK = "lark"


_BY_CHANNEL_ID = {channel_address(channel.value).agent_id: channel for channel in NotifyChannel}


@dataclass(frozen=True)
class Notice:
    """A task's end for the chat it came from: the channel, who to tell there, and the Markdown text."""

    channel: NotifyChannel
    chat: str
    text: str

    def payload(self) -> dict[str, str]:
        return {"channel": self.channel.value, "chat": self.chat, "text": self.text}

    @classmethod
    def from_payload(cls, raw: Any) -> Notice | None:
        """The notice a message carries, or None for none or a malformed one."""
        if not isinstance(raw, Mapping):
            return None
        try:
            channel = NotifyChannel(raw.get("channel"))
        except ValueError:
            return None
        chat, text = raw.get("chat"), raw.get("text")
        if not (isinstance(chat, str) and chat and isinstance(text, str) and text):
            return None
        return cls(channel, chat, text)


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


def lead(summary: str | None) -> str:
    """The start of a summary or closing note for a chat: its first section (up to the next heading: what was
    delivered, or why the task ended), its line breaks kept, at most ``LEAD_CHARS`` characters."""
    text = _NEXT_SECTION.split((summary or "").strip(), maxsplit=1)[0].rstrip()
    return text if len(text) <= LEAD_CHARS else f"{text[:LEAD_CHARS].rstrip()}…"


def notice_text(task_id: str, name: str, status: str, summary: str | None, *, channel: NotifyChannel,
                dashboard_url: str = "") -> str:
    """What ended and how, the start of its summary (or closing note), and the link to the task, as Markdown for
    ``channel``. ``name`` is what the user asked for, else the requirement; the task id when both are empty."""
    flat = " ".join(name.split())
    shown = (flat if len(flat) <= NAME_CHARS else f"{flat[:NAME_CHARS]}…") or task_id
    head = "✅ 任务完成" if db_tasks.task_group(status) is db_tasks.TaskGroup.COMPLETED else "❌ 任务失败"
    lines = [f"{head} · {shown}"]
    if text := lead(summary):
        lines.append(text)
    link = task_link(dashboard_url, task_id)
    # Lark's Markdown links only a link written as one; a Telegram client links a bare URL.
    lines.append(f"[{link}]({link})" if channel is NotifyChannel.LARK else link)
    return "\n".join(lines)
