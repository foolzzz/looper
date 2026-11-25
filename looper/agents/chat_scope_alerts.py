"""Alerts for scoped Manager refusals and invariant violations.

They go to the operator's direct chat (``operator_dm_target``). With no target
or no sender, the text is only logged. Legacy bindings are never a reason to
send. A long resident queue is only a metric.
"""

from __future__ import annotations

import logging
import time
from collections import deque
from collections.abc import Awaitable, Callable
from datetime import datetime

from ..db import system_settings
from ..db.connection import get_pool
from . import session_routing

logger = logging.getLogger(__name__)

Sender = Callable[..., Awaitable[tuple[bool, str | None]]]
_sender: Sender | None = None
_WINDOW_SECONDS = 3600.0
_REFUSAL_THRESHOLD = 10
_RETRY_SECONDS = 300.0
_hits: dict[tuple[str, str], deque[float]] = {}
_retry_since: dict[str, float] = {}
_retry_alerted: set[str] = set()


def configure(sender: Sender | None) -> None:
    """Send alerts through ``sender`` (channel id, chat, text). None: only log them."""
    global _sender
    _sender = sender


def _now() -> datetime:
    return datetime.now().astimezone()


def _mono() -> float:
    return time.monotonic()


def reset() -> None:
    """Drop in-process windows and the sender. Tests use it."""
    global _sender
    _sender = None
    _hits.clear()
    _retry_since.clear()
    _retry_alerted.clear()


def clear_retry(manager_id: str) -> None:
    _retry_since.pop(manager_id, None)
    _retry_alerted.discard(manager_id)


def _scope_label(scope: str | None) -> str:
    return scope if scope else "未解析"


async def _manager_label(manager_id: str) -> str:
    try:
        row = await get_pool().fetchrow("SELECT name FROM managers WHERE id = $1", manager_id)
    except Exception:
        logger.warning("cannot read the Manager name for a scope alert", exc_info=True)
        return manager_id
    name = row["name"] if row is not None else None
    return name.strip() if isinstance(name, str) and name.strip() else manager_id


async def _chat_label(channel_id: str | None, chat: str | None) -> str:
    """The chat's stored name, or the last six characters of its id. Never a person's id."""
    if not chat:
        return "未知会话"
    row = None
    try:
        if channel_id:
            row = await get_pool().fetchrow(
                "SELECT name FROM channel_seen_chats WHERE channel_id = $1 AND chat_id = $2", channel_id, chat)
    except Exception:
        logger.warning("cannot read the chat name for a scope alert", exc_info=True)
    name = row["name"] if row is not None else None
    if isinstance(name, str) and name.strip():
        return name.strip()
    return chat[-6:]


async def _deliver(text: str) -> None:
    logger.warning("chat scope alert: %s", text)
    try:
        from . import operator_identity

        target = await operator_identity.operator_dm_target()
    except Exception:
        logger.warning("cannot resolve the operator direct chat for a scope alert", exc_info=True)
        return
    if target is None or _sender is None:
        return
    try:
        sent, error = await _sender(target.channel_id, target.chat, text)
    except Exception:
        logger.warning("cannot send a scope alert", exc_info=True)
        return
    if not sent:
        logger.warning("cannot send a scope alert to %s: %s", target.channel_id, error)


async def invariant_violation(manager_id: str, where: str, *, channel_id: str | None, chat: str | None,
                              scope: str | None) -> None:
    """Critical. Sent every time, with no claim."""
    try:
        manager = await _manager_label(manager_id)
        label = await _chat_label(channel_id, chat)
        await _deliver(f"严重：Manager {manager} 的会话「{label}」范围检查没有通过（{where}），"
                       f"范围 {_scope_label(scope)}。")
    except Exception:
        logger.warning("cannot send a scope invariant alert", exc_info=True)


async def start_refused(manager_id: str, *, channel_id: str | None, chat: str | None, scope: str | None,
                        runtime: str, mode: str) -> None:
    """High. Once per Manager, chat and scope each local hour."""
    try:
        digest = session_routing.key_digest(f"{manager_id}\n{channel_id}\n{chat}\n{scope}")
        if not await system_settings.claim(f"chat_scope_alert:start:{digest}", _now().strftime("%Y-%m-%dT%H")):
            return
        manager = await _manager_label(manager_id)
        label = await _chat_label(channel_id, chat)
        await _deliver(f"高：Manager {manager} 的会话「{label}」无法用当前 Agent Profile 启动范围 "
                       f"{_scope_label(scope)}（{runtime} / {mode}）。")
    except Exception:
        logger.warning("cannot send a scope start-refusal alert", exc_info=True)


def _crossed(channel_id: str | None, chat: str | None) -> bool:
    now = _mono()
    hits = _hits.setdefault((channel_id or "", chat or ""), deque())
    cutoff = now - _WINDOW_SECONDS
    while hits and hits[0] <= cutoff:
        hits.popleft()
    hits.append(now)
    return len(hits) == _REFUSAL_THRESHOLD


async def out_of_scope(manager_id: str, *, channel_id: str | None, chat: str | None, scope: str | None) -> None:
    """Medium. The tenth refusal in one chat within an hour, once per chat each local day."""
    try:
        if not _crossed(channel_id, chat):
            return
        digest = session_routing.key_digest(f"{channel_id}\n{chat}")
        if not await system_settings.claim(f"chat_scope_alert:out:{digest}", _now().date().isoformat()):
            return
        manager = await _manager_label(manager_id)
        label = await _chat_label(channel_id, chat)
        await _deliver(f"中：Manager {manager} 的会话「{label}」在一小时内有 10 次超出范围 "
                       f"{_scope_label(scope)} 的调用。")
    except Exception:
        logger.warning("cannot send an out-of-scope alert", exc_info=True)


async def resolution_retrying(manager_id: str, *, channel_id: str | None, chat: str | None) -> None:
    """Medium. Once a streak has lasted at least five minutes, until ``clear_retry``."""
    try:
        now = _mono()
        started = _retry_since.get(manager_id)
        if started is None:
            _retry_since[manager_id] = now
            return
        if manager_id in _retry_alerted or now - started < _RETRY_SECONDS:
            return
        _retry_alerted.add(manager_id)
        manager = await _manager_label(manager_id)
        label = await _chat_label(channel_id, chat)
        await _deliver(f"中：Manager {manager} 的范围解析已连续重试超过 5 分钟，会话「{label}」。")
    except Exception:
        logger.warning("cannot send a scope resolution alert", exc_info=True)
