"""/gates, /gate N <answer> and /gate N end: the user answers or ends a gate from a chat, in code, never by the
Manager's model (the Manager intercepts them next to /new).

``/gates`` lists (``LISTED`` per page; ``/gates 2`` is the next page, its numbers go on) the gates that wait for the user: in a private chat (and the web chat) all of them, in every chat
and task, in a group chat that group's. The list is numbered, and the numbers are kept as a snapshot of the last
list sent in that chat (for one sender, ``SNAPSHOT_TTL_SEC``): ``/gate N`` refers to the snapshot, so a number never
lands on a gate other than the one the user saw. ``/gate N <answer>`` adds the answer to gate N as the user's
message, ``/gate N end`` ends the discussion (the Planner closes the gate on what is settled), both through
``gate_service.post_message`` with ``via`` ``command:<channel>``. The replies are code's own text.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from typing import Any

from ..db import tasks as db_tasks
from ..db import user_gates as db_user_gates
from .bus import MessageBus
from .gate_routing import _author, _group
from .gate_service import (
    HTTP_400, HTTP_404, HTTP_409, dashboard_target, gate_chat, origin_chat, post_message, receipt,
)
from .message import AgentAddress, MessageEnvelope, is_lark
from .projects import RequestError

logger = logging.getLogger(__name__)

SNAPSHOT_TTL_SEC = 3600
LISTED = 20
# "/gates", "/gate 2 text", and Telegram's "/gate@bot 2 text".
_COMMAND = re.compile(r"^/(gates?)(?:@\w+)?(?:\s+(.*))?$", re.IGNORECASE | re.DOTALL)
USAGE = "用法：/gates [页码] 列出在等你的关卡；/gate N <回答> 回答第 N 个；/gate N end 按现在的结论结束第 N 个。"
HOW_TO_ANSWER = "回答：/gate N <你的回答>；按现在的结论结束：/gate N end。"
NONE_WAITING = "现在没有在等你的关卡。"


@dataclass(frozen=True)
class Entry:
    """One line of a sent list: the gate the number stands for."""

    number: int
    task_id: str
    label: str
    step_id: str
    name: str


# (channel, chat, sender) -> (when it was sent, its entries)
_snapshots: dict[tuple[str, str, str], tuple[float, list[Entry]]] = {}
# A list is not yet the snapshot while it is being sent: (channel, chat, sender) -> (the list's token, its entries).
# ``settle`` makes it the snapshot once the channel has delivered it; until then, and when it is not delivered, no
# number refers to anything.
_pending: dict[tuple[str, str, str], tuple[str, list[Entry]]] = {}
# The metadata key of an answer that carries a list; its value is the list's token.
SNAPSHOT_TOKEN = "gate_snapshot"


def is_command(text: str) -> bool:
    return _COMMAND.match(text.strip()) is not None


def settle(token: Any, delivered: bool) -> None:
    """The channel has tried to send the list ``token`` names: a delivered list becomes its chat's snapshot, unless
    a newer list was made since; one that was not delivered never does."""
    if not token:
        return
    for key, (pending, entries) in list(_pending.items()):
        if pending == token:
            del _pending[key]
            if delivered:
                _snapshots[key] = (time.monotonic(), entries)


def _waited(moment: Any) -> str:
    from datetime import datetime, timezone

    if not isinstance(moment, datetime):
        return "一会儿"
    seconds = max(0.0, (datetime.now(timezone.utc) - moment).total_seconds())
    if seconds < 3600:
        return f"{int(seconds // 60)} 分钟"
    return f"{int(seconds // 3600)} 小时" if seconds < 172_800 else f"{int(seconds // 86_400)} 天"


def _where(envelope: MessageEnvelope) -> tuple[str, str, str, bool, str]:
    """The channel, the chat, the sender and whether it is a group of a message; the chat as a gate's chat names it."""
    channel = envelope.source.channel or envelope.source.agent_id
    context = envelope.payload.get("channel_context")
    context = context if isinstance(context, dict) else {}
    chat = str(context.get("chat_id") or context.get("session_id") or "")
    sender = str(context.get("sender_id") or context.get("user_id") or "")
    group = (is_lark(channel) or channel == "telegram") and _group(channel, context)
    own = origin_chat(channel, chat, group=group, sender=sender) if is_lark(channel) or channel == "telegram" \
        else chat
    return channel, chat, sender, group, own


async def _waiting(channel: str, own: str, group: bool, limit: int) -> list[dict[str, Any]]:
    if not group:
        return await db_user_gates.open_gates(limit)
    dashboard = await dashboard_target()
    gates = await db_user_gates.open_gates(
        limit, chat=(channel, own),
        dashboard=dashboard is not None and (dashboard.channel.value, dashboard.target.id) == (channel, own))
    here = []
    for gate in gates:
        chat = gate_chat(gate, dashboard)
        if chat is not None and (chat.channel.value, chat.target.id) == (channel, own):
            here.append(gate)
    return here


def _name(gate: dict[str, Any]) -> str:
    return db_tasks.task_name(title=gate.get("task_title"), goal_id=gate.get("task_goal_id"),
                              requirement=gate.get("task_requirement"), task_id=str(gate["task_id"]))


def _entry_text(number: int, gate: dict[str, Any]) -> str:
    lines = [f"{number}. 【{_name(gate)}】{gate['step_key']} {gate['topic']}（已等 {_waited(gate.get('opened_at'))}）"]
    for item in gate.get("agenda") or []:
        if item.get("status") not in (None, "open"):
            continue
        options = item.get("options") or []
        lines.append(f"   · {item.get('question') or item.get('id')}"
                     + (f"（选项：{' / '.join(str(o) for o in options)}）" if options else ""))
    return "\n".join(lines)


async def _list(envelope: MessageEnvelope, page: int = 1) -> str:
    channel, chat, sender, group, own = _where(envelope)
    first = (max(page, 1) - 1) * LISTED
    waiting = await _waiting(channel, own, group, first + LISTED + 1)
    gates, more = waiting[first:first + LISTED], len(waiting) > first + LISTED
    key = (channel, chat, sender)
    _snapshots.pop(key, None)
    _pending.pop(key, None)
    if not gates:
        return NONE_WAITING if first == 0 else f"没有第 {page} 页，最近一次列表以 /gates 为准。"
    numbered = list(enumerate(gates, first + 1))
    _pending[key] = (envelope.message_id, [
        Entry(i, str(g["task_id"]), str(g["step_key"]), str(g["step_id"]), _name(g)) for i, g in numbered])
    next_page = [f"还有更多：/gates {page + 1} 看下一页。"] if more else []
    return "\n".join(["在等你的关卡：", *(_entry_text(i, g) for i, g in numbered), "", *next_page, HOW_TO_ANSWER])


async def _fresh_list(envelope: MessageEnvelope, why: str) -> str:
    return f"{why}\n\n{await _list(envelope)}"


async def _gate(envelope: MessageEnvelope, number: str, answer: str, *, has_planner: Any, bus: MessageBus,
                source: AgentAddress) -> str:
    channel, chat, sender, group, own = _where(envelope)
    key = (channel, chat, sender)
    saved = _snapshots.get(key)
    if saved is not None and time.monotonic() - saved[0] > SNAPSHOT_TTL_SEC:
        saved = _snapshots.pop(key, None) and None
    if saved is None:
        return await _fresh_list(envelope, "请先用 /gates 看一下列表，编号以最近一次列表为准。")
    entries = saved[1]
    index = int(number)
    entry = next((e for e in entries if e.number == index), None)
    if entry is None:
        return await _fresh_list(envelope, f"没有第 {number} 个关卡，编号以最近一次列表为准。")
    end = answer.lower() == "end"
    context = envelope.payload.get("channel_context")
    context = context if isinstance(context, dict) else {}
    chat_origin = is_lark(channel) or channel == "telegram"
    origin = {"author": _author(channel, context) if chat_origin else None, "channel": channel,
              "chat": own if chat_origin else None, "external_id": str(context.get("message_id") or "") or None}
    try:
        posted = await post_message(entry.task_id, entry.label, {"text": "" if end else answer},
                                    has_planner=has_planner(entry.task_id), bus=bus, source=source,
                                    via=f"command:{channel}", end=end, origin=origin)
    except RequestError as exc:
        if exc.status in (HTTP_404, HTTP_409):
            return await _fresh_list(envelope, f"【{entry.name}】的 {entry.label} 已经不在等你了（{exc}）。")
        if exc.status == HTTP_400:
            return f"没能交给 {entry.label}：{exc}"
        raise
    return receipt(entry.name, entry.label, end=end, kept=posted["delivered"] == "kept")


async def handle(envelope: MessageEnvelope, text: str, *, has_planner: Any, bus: MessageBus,
                 source: AgentAddress) -> str | None:
    """The reply to ``/gates`` or ``/gate N ...``, written by code; None when ``text`` is neither."""
    match = _COMMAND.match(text.strip())
    if match is None:
        return None
    word, rest = match.group(1).lower(), (match.group(2) or "").strip()
    if word == "gates":
        if rest and not rest.isdigit():
            return USAGE
        return await _list(envelope, int(rest) if rest else 1)
    number, _, answer = rest.partition(" ")
    answer = answer.strip()
    if not number.isdigit() or not answer:
        return USAGE
    return await _gate(envelope, number, answer, has_planner=has_planner, bus=bus, source=source)
