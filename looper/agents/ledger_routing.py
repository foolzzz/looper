"""Exact ledger replies in an operator DM are code actions, before the Manager model."""

from __future__ import annotations

import logging
import re

from ..db import ledger
from . import gate_routing, operator_identity
from .intent_routing import RouteResult
from .managers_api import DEFAULT_MANAGER_ID
from .message import MessageEnvelope, is_lark
from .ops import Turn
from .ops.ledger import actor_for
from .subscriptions import strip_mentions

_CONFIRM = re.compile(r"确认\s+([Ll]-[0-9]+(?:\s+[Ll]-[0-9]+)*)")
_INVALID = "账本编号无效，请使用 L-0001 这样的正整数编号。"
_SOURCE = {"gate": "请到关卡页处理。", "task": "请到任务页处理。",
           "confirmation": "请回原会话处理待确认的操作。"}
logger = logging.getLogger(__name__)


async def route(envelope: MessageEnvelope) -> RouteResult | None:
    text = envelope.payload.get("text", "")
    match = _CONFIRM.fullmatch(text.strip()) if isinstance(text, str) else None
    if match is None or envelope.target.agent_id != DEFAULT_MANAGER_ID:
        return None
    channel = envelope.source.channel or ""
    context = envelope.payload.get("channel_context")
    context = context if isinstance(context, dict) else {}
    if not (is_lark(channel) or channel == "telegram"):
        return None
    if (gate_routing._group(channel, context)
            or (is_lark(channel) and context.get("chat_type") != "p2p")
            or not context.get("chat_id")):
        return None
    # Forwarded words and replies with quoted context remain model input, never a code command.
    if envelope.payload.get("quote_ref") or any(context.get(key) for key in (
            "forward_origin", "quote", "external_reply", "reply_to_message_id", "reply_to", "root_id", "thread_id")):
        return None
    if not await ledger.enabled():
        return None
    sender = context.get("sender_id") or context.get("user_id")
    client = context.get("client")
    turn = Turn(channel=channel, channel_id=envelope.source.agent_id, chat=str(context["chat_id"]),
                sender=str(sender) if sender else None, group=False, message_id=envelope.message_id,
                client_id=client.get("id") if isinstance(client, dict) else None)
    actor = actor_for(turn, await operator_identity.operator_sender_id(envelope.source.agent_id),
                      manager_id=envelope.target.agent_id)
    if actor.kind != "user":
        return None
    try:
        numbers = [ledger.parse_id(label) for label in match.group(1).split()]
    except ledger.LedgerRefused:
        return RouteResult(_INVALID)
    # Preparation only reads. The Manager executes this after its final stop check.
    return RouteResult(action=lambda: _execute(numbers, actor, text))


def _state_reply(item: dict) -> str | None:
    label = item["id"]
    if item["source"] is not None:
        return f"{label} {_SOURCE[item['source']['kind']]}"
    if item["kind"] == "decision" and item["assurance"] == "user":
        return f"{label} 之前已确认"
    if item["status"] == "done":
        return f"{label} 之前已结项"
    if item["status"] != "open" or item["kind"] not in ("decision", "pending"):
        return f"{label} 当前不能确认，请到账本页处理。"
    return None


async def _execute(numbers: list[int], actor: ledger.Actor, quote: str) -> str:
    replies = []
    for number in numbers:
        # A queued command cannot write after the operator turns the ledger off.
        if not await ledger.enabled():
            replies.append("账本已关闭，未处理后续事项。")
            break
        label = ledger.format_id(number)
        item = await ledger.get_item(number)
        if item is None:
            replies.append(f"没有 {label} 这条账本事项。")
            continue
        state = _state_reply(item)
        if state:
            replies.append(state)
            continue
        action = "confirm" if item["kind"] == "decision" else "close"
        try:
            result = await ledger.apply(action, actor, item_id=number, quote=quote)
            replies.append(f"{label} {'已确认' if action == 'confirm' else '已结项'}：{strip_mentions(result['title'])}")
        except ledger.LedgerRefused as exc:
            # A concurrent confirmation or source change is decided by the writer's locked row.
            current = await ledger.get_item(number)
            replies.append((_state_reply(current) if current else f"没有 {label} 这条账本事项。")
                           or f"{label} 当前不能确认，请到账本页处理。")
            logger.info("ledger DM refused item=%s code=%s", label, exc.code)
    return "\n".join(replies)
