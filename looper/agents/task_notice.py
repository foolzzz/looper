"""Telling the chat a task came from that the task ended (or that its Planner changed a concurrency limit,
``ops.planner``).

Telegram and Lark can send a message unprompted (``ChannelAdapter.notify``),
so a task asked for in one of them is told of its end there, once: when it
completes or fails, the TaskRouter adds a ``Notice`` to the final status it
passes to the Manager, for the ``NotifyTarget`` the task recorded when the
Manager created it (``tasks.chat_id``, ``chat_kind`` and ``chat_mention``): a
Telegram chat, a Lark user (a direct message), or a Lark group with the
requester to @-mention. The Manager sends the notice to the channel instead of
the final summary, on the same path as the task's progress, so it is the
task's last message in that chat. A task without a notice (the web chat, or
one that recorded no chat) gets its final status from the Manager as before;
the dashboard shows the task.
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


class TargetKind(str, Enum):
    """What a notice is sent to (``tasks.chat_kind``)."""

    USER = "user"  # a person, by the id the channel sends to (a Lark open_id): a direct message
    CHAT = "chat"  # a chat, by its id: a Telegram chat, a Lark group (oc_...)


@dataclass(frozen=True)
class NotifyTarget:
    """Who is told of a task's end: a user or a chat, and in a Lark group the requester the notice @-mentions."""

    kind: TargetKind
    id: str
    mention: str | None = None  # an open_id

    def columns(self) -> dict[str, str | None]:
        """The target as the tasks table keeps it."""
        return {"chat_id": self.id, "chat_kind": self.kind.value, "chat_mention": self.mention}

    @classmethod
    def from_columns(cls, row: Mapping[str, Any]) -> NotifyTarget | None:
        """The target a task recorded; None for none (or a malformed one)."""
        return cls._read(row.get("chat_kind"), row.get("chat_id"), row.get("chat_mention"))

    @classmethod
    def _read(cls, kind: Any, chat: Any, mention: Any) -> NotifyTarget | None:
        try:
            kind = TargetKind(kind)
        except ValueError:
            return None
        if not (isinstance(chat, str) and chat):
            return None
        return cls(kind, chat, mention if isinstance(mention, str) and mention else None)


@dataclass(frozen=True)
class Notice:
    """A task's end for the chat it came from: the channel, who to tell there, and the Markdown text."""

    channel: NotifyChannel
    target: NotifyTarget
    text: str

    def payload(self) -> dict[str, str | None]:
        return {"channel": self.channel.value, "chat": self.target.id, "kind": self.target.kind.value,
                "mention": self.target.mention, "text": self.text}

    @classmethod
    def from_payload(cls, raw: Any) -> Notice | None:
        """The notice a message carries, or None for none or a malformed one."""
        if not isinstance(raw, Mapping):
            return None
        try:
            channel = NotifyChannel(raw.get("channel"))
        except ValueError:
            return None
        target, text = NotifyTarget._read(raw.get("kind"), raw.get("chat"), raw.get("mention")), raw.get("text")
        if target is None or not (isinstance(text, str) and text):
            return None
        return cls(channel, target, text)


def notify_channel(origin_channel_id: str | None) -> NotifyChannel | None:
    """The channel a task's ``origin_channel_id`` names, when it is one that is told of the task's end."""
    return _BY_CHANNEL_ID.get(origin_channel_id or "")


def notify_target(origin_channel_id: str | None, *, sender: str | None, chat: str | None,
                  group: bool) -> NotifyTarget | None:
    """Who is told of the end of a task asked for by ``sender`` in ``chat`` (a group chat when ``group``): in
    Telegram the chat; in Lark the sender in a direct message, else the group, @-mentioning the sender. None for a
    channel that cannot be told."""
    match notify_channel(origin_channel_id):
        case NotifyChannel.LARK if group:
            return NotifyTarget(TargetKind.CHAT, chat, mention=sender or None) if chat else None
        case NotifyChannel.LARK:
            return NotifyTarget(TargetKind.USER, sender) if sender else None
        case NotifyChannel.TELEGRAM:
            return NotifyTarget(TargetKind.CHAT, chat) if chat else None
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
                dashboard_url: str = "", extra: str | None = None) -> str:
    """What ended and how, the start of its summary (or closing note), ``extra`` (where a worktree task's work is),
    and the link to the task, as Markdown for ``channel``. ``name`` is what the user asked for, else the
    requirement; the task id when both are empty."""
    flat = " ".join(name.split())
    shown = (flat if len(flat) <= NAME_CHARS else f"{flat[:NAME_CHARS]}…") or task_id
    head = "✅ 任务完成" if db_tasks.task_group(status) is db_tasks.TaskGroup.COMPLETED else "❌ 任务失败"
    lines = [f"{head} · {shown}"]
    if text := lead(summary):
        lines.append(text)
    if extra:
        lines.append(extra)
    lines.append(link_line(task_link(dashboard_url, task_id), channel))
    return "\n".join(lines)


def link_line(link: str, channel: NotifyChannel) -> str:
    """``link`` as a line of a message to ``channel``: Lark's Markdown links only a link written as one; a Telegram
    client links a bare URL."""
    return f"[{link}]({link})" if channel is NotifyChannel.LARK else link
