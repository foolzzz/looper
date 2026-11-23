"""Exact operator memory replies, claimed before the intent router and the Manager model."""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any

from ..db import memory as db_memory
from ..db.connection import get_pool
from ..db.memory import MemoryRefused
from . import gate_routing, memory_actions, memory_model as model, memory_push, memory_settings, memory_write
from . import operator_identity
from .intent_routing import RouteResult
from .message import MessageEnvelope, is_lark

_ID = r"[Mm]-\d{1,18}"
_CONFIRM = re.compile(rf"记\s*({_ID}(?:\s+{_ID})*)")
_REJECT = re.compile(rf"驳回\s*({_ID})(?:\s+(.{{1,300}}))?")
_RETRY = re.compile(rf"重试\s*({_ID})")
_QUOTED_REJECT = re.compile(r"驳回(?:\s+(.{1,300}))?")
# Invalid memory commands are still ours: in particular, intent rejection treats M-ids as free-text reasons.
_CLAIM = re.compile(r"(?:记|驳回|重试)\s*[Mm]-\d")
_FORBIDDEN = "记忆候选只能由你本人在私聊或记忆页处理"
_USAGE = "请回复「记 M-0042」「驳回 M-0042 [理由]」或「重试 M-0042」"
WAIT_SECONDS = 20.0
POLL_SECONDS = .1
_FINISHED = frozenset({"written", "blocked", "conflict", "rejected", "expired", "dropped", "drafting", "pending"})


def _state_reply(row: Mapping[str, Any], action: str) -> str | None:
    label = model.format_id(row["id"])
    if row["state"] in ("written", "rejected"):
        return f"{label} 已写入" if row["state"] == "written" else f"{label} 已驳回"
    if row["state"] in ("confirmed", "writing"):
        return f"{label} 已确认，写入后通知你"
    if action == "confirm" and (row["state"] == "expired" or (
            row["expires_at"] is not None and row["expires_at"] <= datetime.now(timezone.utc))):
        return f"{label} 已过期，请到记忆页恢复：{memory_push.dashboard_link(row['id'])}"
    return None


async def route(envelope: MessageEnvelope) -> RouteResult | None:
    # The stored switch is the only read on the disabled path; never load a vault or inspect a post/channel.
    raw = await memory_settings.stored()
    if raw is None or raw.get("enabled") is not True or not raw.get("push_channel"):
        return None
    text = str(envelope.payload.get("text") or "").strip()
    explicit = bool(_CLAIM.match(text))
    reference = gate_routing.references(envelope)
    quoted = None
    if reference is not None:
        _, channel_id, chat, external_id, _ = reference
        quoted = await get_pool().fetchrow(
            "SELECT * FROM memory_posts WHERE channel_id=$1 AND external_id=$2 AND kind='push'",
            channel_id, external_id)
    if not explicit and quoted is None:
        return None
    channel = envelope.source.channel or ""
    context = envelope.payload.get("channel_context")
    context = context if isinstance(context, dict) else {}
    sender = context.get("sender_id") or context.get("user_id")
    supported = is_lark(channel) or channel == "telegram"
    group = gate_routing._group(channel, context) if supported else True
    # Lark identifies private chats explicitly. An unknown chat kind is not proof of a private chat.
    if is_lark(channel) and context.get("chat_type") != "p2p":
        group = True
    operator = await operator_identity.operator_sender_id(envelope.source.agent_id) if supported else None
    if not supported or not operator_identity.is_operator(
            channel=channel, group=group, sender=str(sender) if sender else None, client_id=None,
            operator_id=operator):
        return RouteResult(_FORBIDDEN)
    confirm, reject, retry = _CONFIRM.fullmatch(text), _REJECT.fullmatch(text), _RETRY.fullmatch(text)
    reason = None
    if confirm:
        action, numbers = "confirm", [model.parse_id(item) for item in confirm.group(1).split()]
    elif reject:
        action, numbers, reason = "reject", [model.parse_id(reject.group(1))], reject.group(2)
    elif retry:
        action, numbers = "retry", [model.parse_id(retry.group(1))]
    elif quoted is not None and text == "记":
        action, numbers = "confirm", [quoted["candidate_id"]]
    elif quoted is not None and (match := _QUOTED_REJECT.fullmatch(text)) and not explicit:
        action, numbers, reason = "reject", [quoted["candidate_id"]], match.group(1)
    else:
        return RouteResult(_USAGE)
    # A Lark push addresses the user by open id; incoming private chat ids are a different (oc_) namespace.
    chat = str(operator) if is_lark(channel) else str(context.get("chat_id") or "")
    if quoted is not None and quoted["chat"] != chat:
        return RouteResult(_FORBIDDEN)
    message_id = str(context.get("message_id") or "")
    if not chat or not message_id:
        return RouteResult(_FORBIDDEN)
    # Preparation is read-only. The Manager's stop check precedes this closure's actions.
    return RouteResult(action=lambda: _execute(
        list(dict.fromkeys(numbers)), action=action, reason=reason, channel_id=envelope.source.agent_id,
        chat=chat, message_id=message_id, via=model.Via.LARK if is_lark(channel) else model.Via.TELEGRAM,
        quoted=quoted if not (confirm or reject or retry) else None))


async def _result_text(row: Mapping[str, Any]) -> str:
    if row["state"] == "written" and not row["note_path"]:
        changes = await get_pool().fetchval(
            "SELECT changes FROM memory_candidate_revisions WHERE candidate_id=$1 AND revision=$2",
            row["id"], row["revision"])
        changes = json.loads(changes) if isinstance(changes, str) else changes or []
        row = {**row, "note_path": "、".join(dict.fromkeys(item["path"] for item in changes))}
    return memory_push.final_text(row)


async def _execute(numbers: list[int], *, action: str, reason: str | None, channel_id: str, chat: str,
                   message_id: str, via: model.Via, quoted: Mapping[str, Any] | None) -> str:
    replies: dict[int, str] = {}
    waiting: set[int] = set()
    reservations: dict[int, int | None] = {}
    for number in numbers:
        label = model.format_id(number)
        row = await get_pool().fetchrow("SELECT * FROM memory_candidates WHERE id=$1", number)
        if row is None:
            replies[number] = f"没有 {label} 这条记忆候选"
            continue
        if state_reply := _state_reply(row, action):
            replies[number] = state_reply
            continue
        post = quoted if quoted is not None and quoted["candidate_id"] == number else None
        if action == "confirm":
            if post is None:
                post = await get_pool().fetchrow(
                    "SELECT * FROM memory_posts WHERE candidate_id=$1 AND kind='push' AND channel_id=$2 "
                    "AND chat=$3 ORDER BY id DESC LIMIT 1", number, channel_id, chat)
            if post is not None and (post["content_hash"] != row["content_hash"] or
                                     post["revision"] != row["revision"]):
                replies[number] = f"{label} 在推送后改过，请看最新的推送"
                continue
            problem = ("尚未在此私聊推送" if post is None else
                       "推送送达情况不明" if post["state"] == "uncertain" else
                       "推送失败" if post["state"] == "failed" else
                       "推送尚未完成" if post["state"] != "sent" else
                       "内容较长" if not post["complete"] else None)
            if problem:
                replies[number] = f"{label} {problem}，请到记忆页确认：{memory_push.dashboard_link(number)}"
                continue
        try:
            # Commit confirmation and its result subscription before waking the writer, whose git transaction
            # holds the candidate row lock. Registering after the wait could otherwise exceed the 20-second bound.
            async with db_memory.transaction() as conn:
                await memory_actions.act(action, label, content_hash=post["content_hash"] if post else None,
                    reason=reason, via=via, post_id=post["id"] if post else None, channel_id=channel_id,
                    chat=chat, message_id=message_id, operator_verified=True, db=conn)
                if action != "reject":
                    current = await conn.fetchrow("SELECT * FROM memory_candidates WHERE id=$1", number)
                    reservations[number] = await memory_push.reserve_result(
                        conn, current, channel_id=channel_id, chat=chat)
        except MemoryRefused as error:
            current = await get_pool().fetchrow("SELECT * FROM memory_candidates WHERE id=$1", number)
            replies[number] = (_state_reply(current, action) if current else None) or (
                f"{label} 在推送后改过，请看最新的推送" if error.code == "stale" else
                f"没有 {label} 这条记忆候选" if error.code == "not_found" else
                f"{label} 当前不能处理，请到记忆页查看：{memory_push.dashboard_link(number)}")
            continue
        if action == "reject":
            current = await get_pool().fetchrow("SELECT * FROM memory_candidates WHERE id=$1", number)
            replies[number] = memory_push.final_text(current)
        else:
            waiting.add(number)
    if waiting:
        memory_write.wake()
        deadline = asyncio.get_running_loop().time() + WAIT_SECONDS
        while waiting:
            rows = await get_pool().fetch("SELECT * FROM memory_candidates WHERE id=ANY($1::bigint[])", list(waiting))
            for row in rows:
                if row["state"] in _FINISHED:
                    await memory_push.settle_result(reservations[row["id"]])
                    replies[row["id"]] = await _result_text(row)
                    waiting.remove(row["id"])
            remaining = deadline - asyncio.get_running_loop().time()
            if not waiting or remaining <= 0:
                break
            await asyncio.sleep(min(POLL_SECONDS, remaining))
        for number in waiting:
            await memory_push.defer_result(number, channel_id=channel_id, chat=chat)
            replies[number] = f"{model.format_id(number)} 已确认，写入后通知你"
    return "\n".join(replies[number] for number in numbers)
