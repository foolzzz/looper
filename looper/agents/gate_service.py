"""The user's side of user gates (``user_gates``): the gates a task page shows, the messages the user writes in
them (from the dashboard, a channel reply or the Manager), the reminders of the gates that wait for the user, and
the chat a gate's messages go to (``gate_chat``: the Lark or Telegram chat its task came from, else the system
setting ``user_gates.dashboard_target``, which ``gate_settings`` shows and changes).

A message is stored first (``db.user_gates.add_user_message``, only while the
gate waits for the user) and its task's Planner is woken (``GATE_MESSAGE``),
which gives it to Claude in a [GATE] turn after the event it is in. A stopped
task's gate keeps its messages for the restart, which gives them. Ending the
discussion is a message too (kind ``end``): the next [GATE] turn must close
the gate. ``GateReminders`` notes, at its project's interval
(``gate_reminder_hours``), each gate that waits for the user, in its
discussion and the task's activity.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any

import json
from dataclasses import dataclass

from ..db import activity as db_activity, system_settings as db_settings, tasks as db_tasks
from ..db import user_gates as db_user_gates
from .bus import MessageBus
from .message import AgentAddress, MessageEnvelope, MessageType, planner_address
from .projects import RequestError
from .task_notice import NotifyChannel, NotifyTarget, TargetKind, notify_channel
from .user_gates import MESSAGE_MAX_CHARS, GateState, MessageKind, retry_at

logger = logging.getLogger(__name__)

HTTP_400 = "400 Bad Request"
HTTP_404 = "404 Not Found"
HTTP_409 = "409 Conflict"
VIA_DASHBOARD = "dashboard"
# How often the reminders look for gates that have waited long enough.
REMINDER_CHECK_SEC = 300
# The system setting naming the chat the gates of a task asked for on the dashboard go to: {"channel", "chat"}.
DASHBOARD_TARGET_KEY = "user_gates.dashboard_target"
_CHANNEL_WORDS = {NotifyChannel.LARK: "Lark", NotifyChannel.TELEGRAM: "Telegram"}


@dataclass(frozen=True)
class GateChat:
    """The chat a gate's messages go to: a channel and who in it (a Lark group @-mentions the requester)."""

    channel: NotifyChannel
    target: NotifyTarget

    @property
    def lark_group(self) -> bool:
        """A Lark group: a reply there reaches the bot only when it @-mentions it, which the messages say."""
        return self.channel is NotifyChannel.LARK and self.target.kind is TargetKind.CHAT

    def label(self) -> str:
        """The chat in words: "Lark group oc_...", "Lark ou_..." (a direct message), "Telegram 123"."""
        group = " group" if self.lark_group else ""
        return f"{_CHANNEL_WORDS[self.channel]}{group} {self.target.id}"


def gate_chat(task: Mapping[str, Any], dashboard: GateChat | None) -> GateChat | None:
    """Where the gates of ``task`` (its row) post: the Lark or Telegram chat it came from (its recorded
    ``NotifyTarget``), else ``dashboard``, the system setting; None for a task a schedule started (it opens no
    gate) or one with neither."""
    if task.get("schedule_id"):
        return None
    channel = notify_channel(task.get("origin_channel_id"))
    if channel is not None:
        target = NotifyTarget.from_columns(task)
        return GateChat(channel, target) if target is not None else None
    return dashboard


def _dashboard_chat(raw: Any) -> GateChat | None:
    """The stored setting as a chat: a Lark user's open_id gets a direct message, a Telegram chat id its chat."""
    try:
        value = json.loads(raw) if isinstance(raw, str) else None
        channel = NotifyChannel(value.get("channel"))
        chat = value.get("chat")
    except (ValueError, AttributeError, TypeError):
        return None
    if not (isinstance(chat, str) and chat):
        return None
    kind = TargetKind.USER if channel is NotifyChannel.LARK else TargetKind.CHAT
    return GateChat(channel, NotifyTarget(kind, chat))


async def dashboard_target() -> GateChat | None:
    """Where the gates of the tasks asked for on the dashboard (or the web chat) post, besides the task page: the
    system setting, None when unset or unreadable."""
    stored = await db_settings.get_settings([DASHBOARD_TARGET_KEY])
    return _dashboard_chat(stored.get(DASHBOARD_TARGET_KEY))


async def gate_settings() -> dict[str, Any]:
    """The setting and the chats it may name: those a channel's sender allowlist already lets in (as a schedule's
    notify target)."""
    from .schedules import notify_targets

    chat = await dashboard_target()
    return {"dashboard_target": {"channel": chat.channel.value, "chat": chat.target.id} if chat else None,
            "targets": await notify_targets()}


async def update_gate_settings(body: Mapping[str, Any]) -> dict[str, Any]:
    """Change ``dashboard_target``: null for the task page only, or ``{channel, chat}``, one of the chats
    ``gate_settings`` offers; 400 for anything else."""
    from .schedules import notify_targets

    if set(body) != {"dashboard_target"}:
        raise RequestError(HTTP_400, "the body is {\"dashboard_target\": null or {\"channel\", \"chat\"}}")
    raw = body["dashboard_target"]
    if raw is None:
        await db_settings.put_settings({DASHBOARD_TARGET_KEY: None})
        return await gate_settings()
    if not isinstance(raw, Mapping) or set(raw) != {"channel", "chat"}:
        raise RequestError(HTTP_400, "dashboard_target is null or {\"channel\", \"chat\"}")
    offered = {(t["channel"], t["chat"]) for t in await notify_targets()}
    if (raw.get("channel"), raw.get("chat")) not in offered:
        raise RequestError(HTTP_400, "dashboard_target must be a chat a channel's sender allowlist lets in (Settings "
                                     "→ Channels)")
    await db_settings.put_settings({DASHBOARD_TARGET_KEY: json.dumps(
        {"channel": raw["channel"], "chat": raw["chat"]})})
    return await gate_settings()


async def task_gate_chat(task: Mapping[str, Any] | None) -> GateChat | None:
    """``gate_chat`` of a task's row, with the dashboard setting read now."""
    if task is None:
        return None
    return gate_chat(task, await dashboard_target() if notify_channel(task.get("origin_channel_id")) is None
                     else None)


def gate_view(row: Mapping[str, Any], chat: GateChat | None = None) -> dict[str, Any]:
    """A gate as the API shows it: its step's label, state, agenda and how it closed, whom it waits for
    (``waiting_on``: ``user``, ``planner`` while one of the user's messages is undelivered and no turn failed on it,
    or the automatic retry of a failed turn is due; None once closed or before it opens), and how many of the user's
    messages a turn failed on and none has taken since (``not_delivered``: the user's next message sends them
    again), with when the Planner tries them again on its own (``retry_at``, None when no retry is left); and, when
    ``chat`` is the chat its messages go to, how many of them were sent there and failed (``channel_posts``, None
    without a chat)."""
    state = GateState(row["state"])
    waiting_on = None
    retry = retry_at(int(row.get("failures") or 0), row.get("failed_at")) if state.waiting else None
    if state.waiting:
        # Past its retry_at, the Planner's automatic retry of a failed turn is due or under way.
        retrying = retry is not None and retry <= datetime.now(timezone.utc)
        waiting_on = "planner" if row.get("waiting_for_planner") or retrying else "user"
    return {
        "step": row.get("step_key"), "step_id": row["step_id"], "state": state.value, "topic": row["topic"],
        "agenda": list(row.get("agenda") or []), "findings": row.get("findings") or "",
        "summary": row.get("summary"), "resolution": row.get("resolution") or "",
        "ended_by_user": bool(row.get("ended_by_user")), "plan_change": row.get("plan_change"),
        "depends_on": list(row.get("depends_on") or []), "waiting_on": waiting_on, "turns": row.get("turns") or 0,
        "not_delivered": int(row.get("not_delivered") or 0) if state.waiting else 0,
        "retry_at": retry,
        "reminders": row.get("reminders") or 0, "opened_at": row.get("opened_at"),
        "first_reply_at": row.get("first_reply_at"), "closed_at": row.get("closed_at"),
        "channel_posts": None if chat is None else {
            "channel": chat.channel.value, "target": chat.label(), "sent": int(row.get("posts_sent") or 0),
            "failed": int(row.get("posts_failed") or 0), "last_error": row.get("post_last_error")},
    }


async def gates_view(task_id: str) -> list[dict[str, Any]]:
    """The task's gates in plan order (``gate_view``)."""
    rows = await db_user_gates.list_for_task(task_id)
    chat = await task_gate_chat(await db_tasks.get_task(task_id)) if rows else None
    return [gate_view(row, chat) for row in rows]


def _message_view(row: Mapping[str, Any]) -> dict[str, Any]:
    return {key: row.get(key) for key in ("id", "role", "kind", "text", "author", "via", "agenda_item", "option",
                                          "created_at")}


async def thread(task_id: str, label: str) -> dict[str, Any]:
    """The discussion of the task's gate ``label``, oldest first; 404 for no such gate."""
    gate = await db_user_gates.get(task_id, label)
    if gate is None:
        raise RequestError(HTTP_404, f"task {task_id} has no gate {label}")
    chat = await task_gate_chat(await db_tasks.get_task(task_id))
    return {"task_id": task_id, "gate": gate_view(gate, chat),
            "messages": [_message_view(row) for row in await db_user_gates.thread(gate["step_id"])]}


def _message(data: Mapping[str, Any], gate: Mapping[str, Any] | None, *, end: bool) -> dict[str, Any]:
    """The message ``data`` asks for, checked: ``text`` (a non-empty string, at most ``MESSAGE_MAX_CHARS``; optional
    when ending), and an option the user chose (``agenda_item``, an item of the gate's agenda, and ``option``,
    one of its options)."""
    text = data.get("text", "" if end else None)
    if not isinstance(text, str) or (not end and not text.strip()):
        raise RequestError(HTTP_400, "text must be a non-empty string")
    text = text.strip()
    if len(text) > MESSAGE_MAX_CHARS:
        raise RequestError(HTTP_400, f"text has {len(text)} characters; at most {MESSAGE_MAX_CHARS}")
    item_id, option = data.get("agenda_item"), data.get("option")
    if (item_id is None) != (option is None) or end and item_id is not None:
        raise RequestError(HTTP_400, "agenda_item and option go together, on a message that is not the end")
    if item_id is not None:
        item = next((i for i in (gate or {}).get("agenda") or [] if i.get("id") == item_id), None)
        if item is None:
            raise RequestError(HTTP_400, f"agenda_item {item_id!r} is not an item of this gate's agenda")
        if option not in (item.get("options") or []):
            raise RequestError(HTTP_400, f"option {option!r} is not one of {item_id}'s options")
    return {"kind": (MessageKind.END if end else MessageKind.MESSAGE).value, "text": text,
            "agenda_item": item_id, "option": option}


async def post_message(
    task_id: str, label: str, data: Mapping[str, Any], *, has_planner: bool, bus: MessageBus, source: AgentAddress,
    via: str = VIA_DASHBOARD, end: bool = False, origin: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Add the user's message (``end``: their end of the discussion) to the task's gate ``label``, and wake its
    Planner when it runs (``has_planner``). ``origin`` is the channel message it is, when it came from one
    (``author`` in a group chat, ``channel``, ``chat``, ``external_id``: the Planner's reply goes under it). 404 for
    no such task or gate, 409 for a gate that does not wait for the user or a task that completed, 400 for a
    message that is not right. ``delivered`` says what happens to it: ``queued`` (the Planner takes it next), or
    ``kept`` (the task is not running: its restart gives it)."""
    task = await db_tasks.get_task(task_id)
    if task is None:
        raise RequestError(HTTP_404, "task not found")
    if db_tasks.task_group(task.get("status")) is db_tasks.TaskGroup.COMPLETED:
        raise RequestError(HTTP_409, "the task has completed: its gates no longer take messages")
    current = await db_user_gates.get(task_id, label)
    if current is None:
        raise RequestError(HTTP_404, f"task {task_id} has no gate {label}")
    message = {**_message(data, current, end=end), "via": via,
               **{key: origin.get(key) for key in ("author", "channel", "chat", "external_id") if origin}}
    gate, added = await db_user_gates.add_user_message(task_id, label, message)
    if gate is None:
        raise RequestError(HTTP_404, f"task {task_id} has no gate {label}")
    if added is None:
        raise RequestError(HTTP_409, f"gate {label} is {gate['state']}: it does not wait for the user")
    if has_planner:
        await bus.publish(MessageEnvelope.create(
            source=source, target=planner_address(task_id), message_type=MessageType.GATE_MESSAGE,
            payload={"task_id": task_id, "gate": label}))
    return {"task_id": task_id, "gate": gate_view(gate, await task_gate_chat(task)), "message": _message_view(added),
            "delivered": "queued" if has_planner else "kept"}


def _hours(since: datetime | None) -> int:
    if since is None:
        return 0
    return int((datetime.now(timezone.utc) - since).total_seconds() // 3600)


class GateReminders:
    """Every ``interval`` seconds, note each gate that has waited for the user for its project's
    ``gate_reminder_hours`` since it was last quiet: a reminder in its discussion and a line in its task's
    activity."""

    def __init__(self, *, interval: float = REMINDER_CHECK_SEC) -> None:
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
                logger.exception("the gate reminders failed; trying again later")
            await asyncio.sleep(self._interval)

    async def run_once(self) -> int:
        """Note every reminder due now; how many."""
        due = await db_user_gates.due_reminders()
        for gate in due:
            waited = _hours(gate.get("opened_at"))
            text = f"Still waiting for you ({waited} h): {gate['topic']}"
            await db_user_gates.record_reminder(gate["step_id"], gate["task_id"], text)
            await db_activity.insert_activity(task_id=gate["task_id"], worker="planner", kind="phase",
                                              summary=f"Gate {gate['step_key']} still waits for the user "
                                                      f"(waited {waited} h); reminder noted"[:500])
        return len(due)
