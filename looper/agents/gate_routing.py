"""A Lark or Telegram message that replies to a user gate's message goes to that gate, before the Manager's model
(user gates RFC, section 9.2).

The adapters put what a message replies to in its ``channel_context``: Lark's
``reply_to`` and ``root_id`` (the thread's first message), Telegram's
``reply_to_message_id``, with the message's own ``message_id``. ``route`` looks
up the message it answers directly (in Lark ``root_id`` only when it names
none: a reply to the Manager's own answer in a gate's thread is the Manager's)
among the messages Looper posted for gates and the user's messages in gates
(``db.user_gates.find_reference``):

- a gate that waits for the user, in a task that has not completed: the message
  is added to it (``gate_service.post_message``: its Planner is woken, or, when
  the task is not running, it is kept for the restart, and the user is told so);
  the Manager's model never sees it;
- a gate that no longer waits, or a completed task: the Manager gets the message
  with a note saying so, and may pass it on as a supplement;
- nothing found: the Manager gets it as before.

In a Lark group only a message that @-mentions the bot reaches Looper at all
(``channels.lark``), so a reply there must @-mention it (the gate's messages
say so).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum
from typing import Any

from ..db import user_gates as db_user_gates
from .bus import MessageBus
from .gate_service import HTTP_409, origin_chat, post_message
from .message import AgentAddress, MessageEnvelope
from .projects import RequestError
from .user_gates import MESSAGE_MAX_CHARS, GateState

logger = logging.getLogger(__name__)

# What the user is told when they answer a gate of a task that is not running.
KEPT_REPLY = "任务已停止，你的回答已保存，任务继续后交给规划者。"
# What the user is told when the gate cannot take their reply as it is.
EMPTY_REPLY = "这条回复没有文字，没能交给关卡：请写下你的回答再回复一次。"
TOO_LONG_REPLY = f"这条回答太长（最多 {MESSAGE_MAX_CHARS} 个字），没能交给关卡：请分几条回复。"
REFUSED_REPLY = "这条回答没能交给关卡，请到任务页查看。"


class RouteKind(str, Enum):
    NONE = "none"  # not a reply to a gate: the Manager answers it
    DELIVERED = "delivered"  # added to the gate; its Planner takes it
    KEPT = "kept"  # added to the gate of a task that is not running: ``Route.reply`` tells the user
    REFUSED = "refused"  # the gate cannot take it as it is (too long): ``Route.reply`` says why
    CLOSED = "closed"  # a reply to a gate that no longer waits: the Manager answers it, with ``Route.note``


@dataclass(frozen=True)
class Route:
    kind: RouteKind
    reply: str = ""  # KEPT, REFUSED: what the user is told
    note: str = ""  # CLOSED: what the Manager is told before the message


NOT_A_REPLY = Route(RouteKind.NONE)


def references(envelope: MessageEnvelope) -> tuple[str, str, str, dict[str, Any]] | None:
    """The channel, the chat and the channel message a Lark or Telegram message replies to, with its context: the
    one it answers directly, and in Lark the first of its thread only when it names none; None for a message from
    elsewhere, or one that replies to nothing."""
    channel = envelope.source.channel or ""
    context = envelope.payload.get("channel_context")
    if channel not in ("lark", "telegram") or not isinstance(context, dict):
        return None
    keys = ("reply_to", "root_id") if channel == "lark" else ("reply_to_message_id",)
    ref = next((str(context[key]) for key in keys if context.get(key)), None)
    chat = str(context.get("chat_id") or "")
    if ref is None or not chat:
        return None
    return channel, chat, ref, context


def _group(channel: str, context: dict[str, Any]) -> bool:
    if channel == "lark":
        return context.get("chat_type") == "group"
    return str(context.get("chat_id") or "").startswith("-")


def _author(channel: str, context: dict[str, Any]) -> str | None:
    """Who wrote it, for the discussion: in a Lark group the sender's name, in a Telegram group their username;
    None in a direct message."""
    name = context.get("sender_name") if channel == "lark" else context.get("username") or context.get("user_id")
    return str(name) if _group(channel, context) and name else None


def _refused(text: str) -> Route:
    """Why the gate cannot take ``text``, in the user's words."""
    if not text:
        return Route(RouteKind.REFUSED, reply=EMPTY_REPLY)
    if len(text) > MESSAGE_MAX_CHARS:
        return Route(RouteKind.REFUSED, reply=TOO_LONG_REPLY)
    return Route(RouteKind.REFUSED, reply=REFUSED_REPLY)


async def route(envelope: MessageEnvelope, *, has_planner: Any, bus: MessageBus, source: AgentAddress) -> Route:
    """Where the user's message goes (module docstring). ``has_planner(task_id)`` says whether the task's Planner
    runs; ``source`` is who wakes it."""
    found_refs = references(envelope)
    if found_refs is None:
        return NOT_A_REPLY
    channel, chat, ref, context = found_refs
    try:
        gate = await db_user_gates.find_reference(channel, chat, [ref])
    except Exception:
        logger.warning("could not look up the gate a %s message replies to", channel, exc_info=True)
        return NOT_A_REPLY
    if gate is None:
        return NOT_A_REPLY
    task_id, label = gate["task_id"], gate["step_key"]
    text = str(envelope.payload.get("text") or "").strip()
    if GateState(gate["state"]).waiting:
        origin = {"author": _author(channel, context), "channel": channel,
                  "chat": origin_chat(channel, chat, group=_group(channel, context),
                                      sender=str(context.get("sender_id") or "")),
                  "external_id": str(context.get("message_id") or "") or None}
        try:
            posted = await post_message(task_id, label, {"text": text}, has_planner=has_planner(task_id), bus=bus,
                                        source=source, via=channel, origin=origin)
        except RequestError as exc:
            if exc.status != HTTP_409:
                logger.info("a %s reply to gate %s of task %s was refused: %s", channel, label, task_id, exc)
                return _refused(text)
            # It stopped waiting meanwhile, or the task completed: the Manager hears it as a closed gate's.
            return _closed(task_id, label, str(exc))
        if posted["delivered"] == "kept":
            return Route(RouteKind.KEPT, reply=KEPT_REPLY)
        return Route(RouteKind.DELIVERED)
    return _closed(task_id, label, f"gate {label} is {gate['state']}")


def _closed(task_id: str, label: str, why: str) -> Route:
    return Route(RouteKind.CLOSED, note=(
        f"[Looper] This message replies to a message of gate {label} of task {task_id}, which no longer takes "
        f"answers ({why}). If what the user says still matters to the task, pass it on with looper_task_instruct."))
