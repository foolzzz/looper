"""Posting a user gate's discussion to the chat its task came from (user gates RFC, section 9.1).

The Planner and the reminders only write a gate's discussion (``db.user_gates``).
``GateCourier``, in ``looper serve``, posts each message of the Planner's and
Looper's that no pass has handled yet (``post_state``), oldest first, to the
gate's chat (``gate_service.gate_chat``) through the channel's ``post``, and
notes what that did: the channel messages it became (``task_user_gate_posts``;
a reply to any of them is about the gate, ``gate_routing``), why it failed
(the task page shows it, and the activity), or that the gate has no chat. The
card (the gate opened) is a message of its own; every later message replies
under it, but a Planner's reply goes under the user's message it answers when
that came from the gate's chat. The texts are code's, in Chinese like a task's
end notice; a Planner's reply goes as it wrote it. In a Lark group a reply
reaches the bot only when it @-mentions it, so what waits for an answer there
says so.

A post that sent nothing is tried again, after ``POST_RETRY_SEC``, up to
``POST_ATTEMPTS`` in all, and the gate's later messages wait behind it; only
then is it ``failed``. Each post carries an idempotency key of its message
(Lark sends one message per key; Telegram has none, so a post retried after a
lost answer may arrive twice), and a reply whose message is gone is posted on
its own. A card, reminder, update or "still waits" message whose gate no longer
waits by the time it is posted (after a backlog) is not posted.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Mapping, Sequence
from datetime import datetime, timedelta, timezone
from typing import Any

from ..db import activity as db_activity, tasks as db_tasks, user_gates as db_user_gates
from .channels.base import PostReceipt
from .gate_service import GateChat, dashboard_target, gate_chat
from .links import task_link
from .task_notice import NAME_CHARS, NotifyTarget, link_line
from .user_gates import RETRY_WAIT_SEC, GateState, MessageKind

logger = logging.getLogger(__name__)

# How often the courier looks for messages to post.
POST_CHECK_SEC = 2.0
# What one pass takes at most; the rest wait for the next.
POST_BATCH = 50
FINDINGS_CHARS = 300
QUESTION_CHARS = 200
ANSWER_LINE = "直接回复这条消息作答。"
GROUP_LINE = "在群里回复请 @机器人。"
# Tries of a post that sends nothing, and the waits between them: about ten minutes in all.
POST_ATTEMPTS = 3
POST_RETRY_SEC = (120.0, 480.0)
# The kinds that ask for an answer: not posted once the gate no longer waits.
WHILE_WAITING = frozenset({MessageKind.OPENED, MessageKind.UPDATED, MessageKind.REMINDER, MessageKind.RESUMED})

# Posts ``text`` to ``target`` in the channel named (a reply to ``reply_to`` when given): ``ChannelRegistry.post``.
Poster = Callable[..., Awaitable[PostReceipt]]


def _clip(text: Any, limit: int) -> str:
    flat = " ".join(str(text or "").split())
    return flat if len(flat) <= limit else f"{flat[:limit - 1]}…"


def _name(row: Mapping[str, Any]) -> str:
    """The task's name for a chat: what the user asked for, short."""
    name = db_tasks.task_name(title=row.get("title"), goal_id=row.get("goal_id"), requirement=row.get("requirement"),
                              task_id=row["task_id"])
    return _clip(name, NAME_CHARS)


def _questions(agenda: Sequence[Mapping[str, Any]]) -> list[str]:
    """The questions still open, numbered, with their options."""
    lines = []
    for item in agenda:
        if item.get("status") not in (None, "open"):
            continue
        options = [str(o) for o in item.get("options") or []]
        choice = f"（选项：{' / '.join(options)}）" if options else ""
        lines.append(f"{len(lines) + 1}. {_clip(item.get('question'), QUESTION_CHARS)}{choice}")
    return lines


def _hours_since(moment: datetime | None) -> int:
    if moment is None:
        return 0
    return int((datetime.now(timezone.utc) - moment).total_seconds() // 3600)


def _summary_lines(summary: Mapping[str, Any] | None, plan_change: Mapping[str, Any] | None) -> list[str]:
    lines: list[str] = []
    for key, heading in (("decisions", "决定"), ("assumptions", "假设（没有经你确认）"), ("open", "仍开着")):
        items = [str(i) for i in (summary or {}).get(key) or []]
        if items:
            lines += [f"{heading}：", *(f"- {_clip(i, QUESTION_CHARS)}" for i in items)]
    change = [f"{word} {', '.join(labels)}" for key, word in (("added", "加了"), ("dropped", "去掉了"),
                                                              ("stopped", "停掉了"))
              if (labels := [str(label) for label in (plan_change or {}).get(key) or []])]
    if change:
        lines.append(f"计划：{'；'.join(change)}")
    return lines


def message_text(row: Mapping[str, Any], chat: GateChat, *, dashboard_url: str = "") -> str | None:
    """What a message of the gate's discussion (``row``, from ``db.user_gates.unposted``) says in ``chat``; None
    for one that is not posted (the user's own)."""
    try:
        kind = MessageKind(row["kind"])
    except ValueError:
        return None
    waiting = GateState(row["state"]).waiting
    group = [GROUP_LINE] if chat.lark_group and waiting else []
    link = link_line(task_link(dashboard_url, row["task_id"]), chat.channel)
    agenda = row.get("agenda") or []
    findings = [f"目前的发现：{_clip(row['findings'], FINDINGS_CHARS)}"] if row.get("findings") else []
    match kind:
        case MessageKind.OPENED:
            lines = [f"⏸ 需要你决定 · {_name(row)}", _clip(row["topic"], QUESTION_CHARS), *_questions(agenda),
                     *findings, ANSWER_LINE, *group, link]
        case MessageKind.REPLY:
            lines = [str(row.get("text") or "").strip(), *group]
        case MessageKind.UPDATED:
            note = f"：{_clip(row.get('text'), QUESTION_CHARS)}" if row.get("text") else ""
            lines = [f"✏️ 规划者更新了问题{note}", *_questions(agenda), *findings, *group]
        case MessageKind.REMINDER:
            lines = [f"⏰ 还在等你 · 已等 {_hours_since(row.get('opened_at'))} 小时 · {_name(row)}",
                     _clip(row["topic"], QUESTION_CHARS), *_questions(agenda), ANSWER_LINE, *group, link]
        case MessageKind.CANCELLED:
            lines = [f"✅ 不用回答了：{_clip(row.get('resolution') or row.get('text'), QUESTION_CHARS)}"]
        case MessageKind.CLOSED:
            lines = [f"✅ 讨论结束 · {_clip(row['topic'], QUESTION_CHARS)}",
                     *_summary_lines(row.get("summary"), row.get("plan_change")), link]
        case MessageKind.RESUMED:
            lines = [f"▶️ 任务继续了，这个问题仍在等你：{_clip(row['topic'], QUESTION_CHARS)}", *group]
        case MessageKind.ERROR:
            failures = int(row.get("failures") or 0)
            if 0 < failures <= len(RETRY_WAIT_SEC):
                again = (f"约 {max(1, round(RETRY_WAIT_SEC[failures - 1] / 60))} 分钟后会自动重试，"
                         "你的下一条消息也会一起交给它。")
            else:
                again = "你的下一条消息会把它一起交给规划者。"
            lines = [f"⚠️ 规划者没能处理你的消息：{again}", *group]
        case MessageKind.LATE:
            lines = ["你在讨论结束时发的消息已作为补充说明交给规划者。"]
        case _:
            return None
    text = "\n".join(line for line in lines if line)
    return text or None


def _task_columns(row: Mapping[str, Any]) -> dict[str, Any]:
    """The task's columns ``gate_chat`` reads, from a message's row."""
    return {"origin_channel_id": row.get("origin_channel_id"), "schedule_id": row.get("schedule_id"),
            "chat_id": row.get("task_chat_id"), "chat_kind": row.get("chat_kind"),
            "chat_mention": row.get("chat_mention")}


class GateCourier:
    """Every ``interval`` seconds, post the gates' messages no pass has handled (``post``: ``ChannelRegistry.post``)
    and note what each did. The dashboard's target is read each pass, so a change to it applies at once."""

    def __init__(self, post: Poster, *, dashboard_url: str = "", interval: float = POST_CHECK_SEC) -> None:
        self._post = post
        self._dashboard_url = dashboard_url
        self._interval = interval
        self._task: asyncio.Task[Any] | None = None

    async def start(self) -> None:
        self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def _loop(self) -> None:
        while True:
            try:
                await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("the gate courier failed; trying again later")
            await asyncio.sleep(self._interval)

    async def run_once(self) -> int:
        """Post the messages waiting now; how many were handled."""
        rows = await db_user_gates.unposted(POST_BATCH)
        if not rows:
            return 0
        dashboard = await dashboard_target()
        held: set[str] = set()  # gates whose message waits for its next try: their later messages wait too
        for row in rows:
            if row["step_id"] in held:
                continue
            if not await self._handle(row, dashboard):
                held.add(row["step_id"])
        return len(rows)

    async def _handle(self, row: Mapping[str, Any], dashboard: GateChat | None) -> bool:
        """Post one message and note what that did; False when it waits for its next try."""
        chat = gate_chat(_task_columns(row), dashboard)
        text = message_text(row, chat, dashboard_url=self._dashboard_url) if chat is not None else None
        stale = text is not None and MessageKind(row["kind"]) in WHILE_WAITING and not GateState(row["state"]).waiting
        if chat is None or text is None or stale:
            await db_user_gates.record_post(row["id"], row["task_id"], "none")
            return True
        reply_to = await self._reply_to(row, chat)
        key = f"gate-{row['id']}"
        receipt = await self._post(chat.channel.value, chat.target, text, reply_to=reply_to, idempotency_key=key)
        if receipt.parent_gone:
            # The message it replies to was recalled (or deleted, which Lark counts as recalled): post it on its own.
            receipt = await self._post(chat.channel.value, chat.target, text, reply_to=None,
                                       idempotency_key=f"{key}-own")
        attempt = int(row.get("post_attempts") or 0) + 1
        if not receipt.success and not receipt.external_ids and attempt < POST_ATTEMPTS:
            await db_user_gates.record_retry(row["id"], receipt.error or "not sent", datetime.now(timezone.utc)
                                             + timedelta(seconds=POST_RETRY_SEC[attempt - 1]))
            return False
        await db_user_gates.record_post(row["id"], row["task_id"], "sent" if receipt.success else "failed",
                                        error=None if receipt.success else receipt.error or "not sent",
                                        channel=chat.channel.value, chat=chat.target.id,
                                        external_ids=receipt.external_ids)
        if not receipt.success:
            await db_activity.insert_activity(
                task_id=row["task_id"], worker="planner", kind="error",
                summary=f"Gate {row['step_key']}: could not send to {chat.label()}: {receipt.error}"[:500])
        return True

    @staticmethod
    async def _reply_to(row: Mapping[str, Any], chat: GateChat) -> str | None:
        """What the message replies to in ``chat``: nothing for the card; the user's message a Planner's reply (or
        a note on that message) answers, when it came from this chat (one the user sent the Manager from another
        chat is not there); else the gate's first message there (its card)."""
        if row["kind"] == MessageKind.OPENED.value:
            return None
        if row["kind"] in (MessageKind.REPLY.value, MessageKind.ERROR.value, MessageKind.LATE.value):
            user = await db_user_gates.answered(row["step_id"], row["id"])
            if user and user.get("external_id") and _same_chat(user, chat.channel.value, chat.target):
                return str(user["external_id"])
        return await db_user_gates.first_post(row["step_id"], chat.channel.value, chat.target.id)


def _same_chat(message: Mapping[str, Any], channel: str, target: NotifyTarget) -> bool:
    """Whether the user's ``message`` came from the chat posted to, as ``gate_service.origin_chat`` names it (a
    Lark direct message by its sender's open_id): a reply goes into the chat of the message it replies to."""
    return message.get("channel") == channel and str(message.get("chat") or "") == target.id
