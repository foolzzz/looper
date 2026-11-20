"""Exact operator replies act on intents before any message reaches the Manager model."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from ..db.connection import get_pool
from ..db.intents import IntentRefused
from . import gate_routing, intents, leads_model, operator_identity
from .message import MessageEnvelope, is_lark
from .subscriptions import strip_mentions

_BUILD = re.compile(r"建(?:\s*(I-?\d+))?(?:\s+(\d{1,2}))?", re.IGNORECASE)
_REJECT = re.compile(r"驳回(?:\s*(I-?\d+))?(?:\s+(.{1,200}))?", re.IGNORECASE)
_FORBIDDEN = "意图只能由你本人在私聊或意图页处理。"
logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RouteResult:
    reply: str


def _state_reply(row: dict[str, Any]) -> str | None:
    label = leads_model.format_intent_id(row["id"])
    match row["state"]:
        case "tasked":
            return f"{label} 已经建过任务 {row['task_id']}。"
        case "rejected":
            return f"{label} 已驳回过。"
        case "merged":
            return f"{label} 已合并到 {leads_model.format_intent_id(row['merged_into'])}，请处理那一条。"
    return None


def _needs_project(label: str, candidates: list[dict[str, Any]]) -> str:
    if not candidates:
        return "请到意图页选择项目。"
    names = "  ".join(f"{index}. {strip_mentions(item['name'])}" for index, item in enumerate(candidates, 1))
    return f"项目：这个会话没有绑定项目。候选：{names}\n回复「建 {label} <编号>」"


async def route(envelope: MessageEnvelope, *, platform: Any) -> RouteResult | None:
    if not await leads_model.intents_enabled():
        return None
    text = " ".join(str(envelope.payload.get("text") or "").split())
    build = _BUILD.fullmatch(text)
    match = build or _REJECT.fullmatch(text)
    if match is None:
        return None
    pool = get_pool()
    explicit = match.group(1)
    if explicit:
        # The command grammar also accepts I42; foundation's parser intentionally accepts I-42 only.
        digits = re.sub(r"^[Ii]-?", "", explicit).lstrip("0") or "0"
        number = int(digits) if len(digits) <= 19 else None
        if number is not None and number > 9223372036854775807:
            number = None
        label = f"I-{digits.zfill(4)}"
    else:
        references = gate_routing.references(envelope)
        if references is None:
            return None
        _, channel_id, _, external_id, _ = references
        row = await pool.fetchrow("SELECT i.id, i.state, i.merged_into FROM intent_posts p "
            "JOIN intents i ON i.id=p.intent_id WHERE p.channel_id=$1 AND p.external_id=$2",
            channel_id, external_id)
        if row is None:
            return None
        number = row["merged_into"] if row["state"] == "merged" else row["id"]
        label = leads_model.format_intent_id(number)
    channel = envelope.source.channel or ""
    context = envelope.payload.get("channel_context")
    context = context if isinstance(context, dict) else {}
    group = gate_routing._group(channel, context) if is_lark(channel) or channel == "telegram" else False
    sender = context.get("sender_id") or context.get("user_id")
    client = context.get("client")
    client_id = client.get("id") if isinstance(client, dict) else None
    operator = await operator_identity.operator_sender_id(envelope.source.agent_id) if channel != "web" else None
    if not operator_identity.is_operator(channel=channel, group=group, sender=str(sender) if sender else None,
                                        client_id=client_id, operator_id=operator):
        return RouteResult(_FORBIDDEN)
    if number is None:
        return RouteResult(f"没有 {label} 这条意图。")
    row = await pool.fetchrow("SELECT * FROM intents WHERE id=$1", number)
    if row is None:
        return RouteResult(f"没有 {label} 这条意图。")
    if build and row["state"] == "tasked":
        try:
            await intents.resume_start(dict(row), platform=platform)
        except Exception:
            logger.exception("intent task start retry failed intent=%s task=%s", number, row["task_id"])
            return RouteResult(f"任务 {row['task_id']} 已建，但启动请求失败，请重试「建 {label}」。")
    state_reply = _state_reply(dict(row))
    if state_reply:
        return RouteResult(state_reply)
    via = "dashboard" if channel == "web" else "lark" if is_lark(channel) else "telegram"
    origin = None if channel == "web" else {
        "channel": channel, "channel_id": envelope.source.agent_id,
        "chat": str(context.get("chat_id") or ""), "sender": str(sender) if sender else None,
        "group": group, "manager_id": envelope.target.agent_id, "correlation_id": envelope.correlation_id,
        "message_id": context.get("message_id")}
    try:
        if build:
            result = await intents.build(number, via=via, origin=origin,
                candidate=int(match.group(2)) if match.group(2) else None, now=datetime.now(timezone.utc),
                platform=platform)
            return RouteResult(strip_mentions(
                f"已建任务 {result['task_id']}：{result['intent']['title']}，已开始。"))
        await intents.reject(number, via=via, reason=match.group(2))
        return RouteResult(f"{label} 已驳回。")
    except intents.NeedsProject as exc:
        return RouteResult(_needs_project(label, exc.candidates))
    except intents.NeedsDirectory as exc:
        return RouteResult(f"项目 {strip_mentions(exc.project['name'])} 没有默认目录，请到意图页建任务。")
    except intents.CandidateOutOfRange as exc:
        return RouteResult(f"没有第 {exc.candidate} 个候选项目。")
    except IntentRefused as exc:
        # Another operator turn can have handled it since our read; the transition's locked row wins.
        if exc.code == "not_found":
            return RouteResult(f"没有 {label} 这条意图。")
        state_reply = _state_reply(exc.row) if exc.row else None
        return RouteResult(state_reply or strip_mentions(str(exc)))
