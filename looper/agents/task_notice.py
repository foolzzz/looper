"""Telling the chat a task came from that the task ended (or that its Planner changed a concurrency limit,
``ops.planner``).

Telegram and Lark can send a message unprompted (``ChannelAdapter.notify``),
so a task asked for in one of them is told of its end there, once: when it
completes or fails, the TaskRouter adds a ``Notice`` to the final status it
passes to the Manager, for the ``NotifyTarget`` the task recorded when the
Manager created it (``tasks.chat_id``, ``chat_kind``, ``chat_mention`` and
``chat_group``): a Telegram chat, a Lark user (a direct message), or a Lark
group with the requester to @-mention. The Manager sends the notice to the
channel instead of the final summary, on the same path as the task's progress,
so it is the task's last message in that chat. A group chat (``group``) gets
only the notice: its members asked a question, and the task's breakdown,
progress and phase reports are the operator's (the dashboard shows them). A
task without a notice (the web chat, or one that recorded no chat) gets its
final status from the Manager as before; the dashboard shows the task.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
from typing import Any

from ..db import tasks as db_tasks
from .acceptance_result import ResultEvidence
from .links import is_public, task_link
from .message import builtin_channel_id, is_lark, parse_channel_id

NAME_CHARS = 40
LEAD_CHARS = 300
# A group notice is sent in messages of at most this many characters (Telegram's own limit is 4,096).
GROUP_MESSAGE_CHARS = 4000
# The start of the second section of a Markdown summary: a heading line after the first line.
_NEXT_SECTION = re.compile(r"\n(?=#{1,6} )")


class NotifyChannel(str, Enum):
    """A channel whose adapter implements ``ChannelAdapter.notify``, and so is told when a task asked for in it
    ends."""

    TELEGRAM = "telegram"
    LARK = "lark"
    LARK_BOT = "lark_bot"

    @property
    def is_lark(self) -> bool:
        return is_lark(self.value)


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
    group: bool = False  # the chat is a group chat (people besides the requester read it)

    def columns(self) -> dict[str, str | bool | None]:
        """The target as the tasks table keeps it."""
        return {"chat_id": self.id, "chat_kind": self.kind.value, "chat_mention": self.mention,
                "chat_group": self.group}

    @classmethod
    def from_columns(cls, row: Mapping[str, Any]) -> NotifyTarget | None:
        """The target a task recorded; None for none (or a malformed one)."""
        return cls._read(row.get("chat_kind"), row.get("chat_id"), row.get("chat_mention"), row.get("chat_group"))

    @classmethod
    def _read(cls, kind: Any, chat: Any, mention: Any, group: Any = False) -> NotifyTarget | None:
        try:
            kind = TargetKind(kind)
        except ValueError:
            return None
        if not (isinstance(chat, str) and chat):
            return None
        return cls(kind, chat, mention if isinstance(mention, str) and mention else None, group is True)


@dataclass(frozen=True)
class Notice:
    """A task's end for the chat it came from: the channel, who to tell there, and the Markdown text."""

    channel: NotifyChannel
    target: NotifyTarget
    text: str
    channel_id: str  # the bot's explicit channel id

    def __post_init__(self) -> None:
        if not isinstance(self.channel, NotifyChannel):
            raise ValueError("notice channel must be a NotifyChannel")
        if not isinstance(self.channel_id, str) or not self.channel_id:
            raise ValueError("notice channel_id must be a nonempty string")
        parsed = parse_channel_id(self.channel_id)
        if parsed is None or parsed.channel_type != self.channel.value:
            raise ValueError(f"channel id {self.channel_id!r} is not a {self.channel.value} bot")

    def payload(self) -> dict[str, str | bool | None]:
        return {"channel": self.channel.value, "channel_id": self.channel_id, "chat": self.target.id,
                "kind": self.target.kind.value, "mention": self.target.mention, "group": self.target.group,
                "text": self.text}

    @classmethod
    def from_payload(cls, raw: Any) -> Notice | None:
        """The notice a message carries, or None for none or a malformed one."""
        if not isinstance(raw, Mapping):
            return None
        try:
            channel = NotifyChannel(raw.get("channel"))
        except ValueError:
            return None
        target, text = NotifyTarget._read(raw.get("kind"), raw.get("chat"), raw.get("mention"), raw.get("group")), raw.get("text")
        if target is None or not (isinstance(text, str) and text):
            return None
        channel_id = raw.get("channel_id", builtin_channel_id(channel.value))
        parsed = parse_channel_id(channel_id) if isinstance(channel_id, str) else None
        if parsed is None or parsed.channel_type != channel.value:
            return None
        return cls(channel, target, text, channel_id=channel_id)


def notify_channel(origin_channel_id: str | None) -> NotifyChannel | None:
    """The channel a task's ``origin_channel_id`` names, when it is one that is told of the task's end."""
    parsed = parse_channel_id(origin_channel_id)
    return NotifyChannel(parsed.channel_type) if parsed is not None else None


def notify_target(origin_channel_id: str | None, *, sender: str | None, chat: str | None,
                  group: bool) -> NotifyTarget | None:
    """Who is told of the end of a task asked for by ``sender`` in ``chat`` (a group chat when ``group``): in
    Telegram the chat; in Lark the sender in a direct message, else the group, @-mentioning the sender. None for a
    channel that cannot be told."""
    match notify_channel(origin_channel_id):
        case channel if channel is not None and channel.is_lark and group:
            return NotifyTarget(TargetKind.CHAT, chat, mention=sender or None, group=True) if chat else None
        case channel if channel is not None and channel.is_lark:
            return NotifyTarget(TargetKind.USER, sender) if sender else None
        case NotifyChannel.TELEGRAM:
            return NotifyTarget(TargetKind.CHAT, chat, group=group) if chat else None
    return None


def ends_task(status: str | None) -> bool:
    """Whether a Planner's status is its task's end: completed, or failed (``db.tasks.PLANNER_END_STATUSES``)."""
    return isinstance(status, str) and status in db_tasks.PLANNER_END_STATUSES


def web_end_text(status: str, title: str, summary: str, delivery_summary: str = "") -> str:
    """What a web chat session is told when its task ended: the summary (clipped to 2,000 characters around a
    marker that keeps its tail, the delivery summary appended once after it), or that the task was cancelled."""
    if delivery_summary:
        # The persisted result includes the appendix; append it once, outside the chat's result clip.
        summary = summary.removesuffix("\n\n" + delivery_summary)
    if len(summary) > 2000:
        # Reserve marker space inside the existing budget, retaining the report's tail.
        tail = summary[-600:]
        marker = f'\n… ({len(summary)} characters omitted; full result on the task page)\n'
        head_size = 2000 - len(tail) - len(marker)
        omitted = len(summary) - head_size - len(tail)
        marker = f'\n… ({omitted} characters omitted; full result on the task page)\n'
        summary = summary[:head_size] + marker + tail
    if delivery_summary:
        summary += "\n\n" + delivery_summary
    if status == "completed":
        return f"任务「{title}」已完成：{summary}"
    if status == "failed":
        return f"任务「{title}」失败：{summary}"
    return f"任务「{title}」已取消。"


def lead(summary: str | None) -> str:
    """The start of a summary or closing note for a chat: its first section (up to the next heading: what was
    delivered, or why the task ended), its line breaks kept, at most ``LEAD_CHARS`` characters."""
    text = _NEXT_SECTION.split((summary or "").strip(), maxsplit=1)[0].rstrip()
    return text if len(text) <= LEAD_CHARS else f"{text[:LEAD_CHARS].rstrip()}…"


def notice_text(task_id: str, name: str, status: str, summary: str | None, *, channel: NotifyChannel,
                dashboard_url: str = "", extra: str | None = None, delivery_summary: str | None = None,
                group: bool = False, evidence: ResultEvidence | None = None) -> str:
    """What ended and how, the start of its summary (or closing note), ``extra`` (where a worktree task's work is),
    and the link to the task, as Markdown for ``channel``. ``name`` is what the user asked for, else the
    requirement; the task id when both are empty. In a group chat (``group``) the whole summary, which the people
    there cannot open the task page to read, and the link only to a public dashboard (``task_link_line``)."""
    flat = " ".join(name.split())
    shown = (flat if len(flat) <= NAME_CHARS else f"{flat[:NAME_CHARS]}…") or task_id
    head = "✅ 任务完成" if db_tasks.task_group(status) is db_tasks.TaskGroup.COMPLETED else "❌ 任务失败"
    lines = [f"{head} · {shown}"]
    if group:
        # The persisted summary already ends with the delivery summary, which follows once below.
        whole = (summary or "").strip()
        if evidence is not None:
            whole = whole.replace("\n\n" + evidence.section, "")
        text = whole.removesuffix(delivery_summary).rstrip() if delivery_summary else whole
    else:
        text = lead(summary)
    if text:
        lines.append(text)
    if delivery_summary:
        lines.append(delivery_summary)
    if evidence is not None and (count_line := evidence.counts.line()):
        lines.append(count_line)
    if extra:
        lines.append(extra)
    if link := task_link_line(dashboard_url, task_id, channel, group=group):
        lines.append(link)
    return "\n".join(lines)


def task_link_line(base: str, task_id: str, channel: NotifyChannel, *, group: bool) -> str | None:
    """The line linking ``task_id``'s page in a message to ``channel``; None in a group chat when ``base`` is not a
    public address: the people there cannot open the server's own."""
    if group and not is_public(base):
        return None
    return link_line(task_link(base, task_id), channel)


def split_message(text: str, limit: int = GROUP_MESSAGE_CHARS) -> list[str]:
    """``text`` as messages of at most ``limit`` characters, in order, cut between lines (a single line longer than
    ``limit`` is cut where it must be). A cut inside a code fence closes the fence and reopens it in the next
    message, so each renders on its own."""
    if len(text) <= limit:
        return [text]
    room = limit - len("\n```")  # for the fence a cut inside one closes
    parts: list[str] = []
    current: list[str] = []
    size = 0
    fence: str | None = None  # the opening line of the fence the next line is in

    def flush() -> None:
        nonlocal current, size
        if fence is not None:
            current.append("```")
        parts.append("\n".join(current))
        current, size = ([fence] if fence is not None else []), (len(fence) if fence is not None else 0)

    for line in text.split("\n"):
        for piece in [line[i:i + room] for i in range(0, len(line), room)] or [""]:
            if current and size + 1 + len(piece) > room:
                flush()
            size += len(piece) + (1 if current else 0)
            current.append(piece)
        if line.lstrip().startswith("```"):
            fence = None if fence is not None else line.strip()
    if current and (len(current) > 1 or current[0] != fence):
        parts.append("\n".join(current))
    return parts


def link_line(link: str, channel: NotifyChannel) -> str:
    """``link`` as a line of a message to ``channel``: Lark's Markdown links only a link written as one; a Telegram
    client links a bare URL."""
    return f"[{link}]({link})" if channel.is_lark else link
