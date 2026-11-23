"""Default-off, best-effort observations of turns and proactive deliveries."""
from __future__ import annotations

import asyncio
from collections import OrderedDict
from collections.abc import Coroutine
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
import logging
import time
from typing import Any

from . import chat_settings
from .ops import TurnOrigin

logger = logging.getLogger(__name__)


class TurnDelivery(str, Enum):
    REPLY = 'reply'
    EMPTY = 'empty'
    FAILED = 'failed'
    STOPPED = 'stopped'
    OVER_CAP = 'over_cap'


class AckKind(str, Enum):
    REACTION = 'reaction'
    STATUS = 'status'
    NONE = 'none'
    PENDING = 'pending'


class ChatKind(str, Enum):
    DASHBOARD = 'dashboard'
    CLIENT = 'client'
    DIRECT = 'direct'
    GROUP = 'group'


@dataclass
class TurnTimes:
    received_at: datetime
    ack_at: datetime | None = None
    ack: AckKind = AckKind.PENDING
    first_reply_at: datetime | None = None
    tool_calls: int = 0
    chat: ChatKind = ChatKind.DASHBOARD

    def to_metadata(self, delivery: TurnDelivery, origin: TurnOrigin, replayed: bool) -> dict[str, Any]:
        if origin is TurnOrigin.COMPLETION:
            return {'origin': origin.value, 'tool_calls': self.tool_calls, 'delivery': delivery.value}
        def elapsed(at: datetime | None) -> int | None:
            return None if at is None else round((at - self.received_at).total_seconds() * 1000)
        return {'v': 1, 'origin': origin.value, 'chat': self.chat.value,
                'received_at': self.received_at.isoformat().replace('+00:00', 'Z'),
                'ack': self.ack.value, 'ack_ms': elapsed(self.ack_at),
                'first_reply_ms': elapsed(self.first_reply_at), 'tool_calls': self.tool_calls,
                'delivery': delivery.value, 'replayed': replayed}


class AckBoard:
    """A missing entry is pending; an explicit None means no reaction was delivered."""
    def __init__(self, max_entries: int = 2000) -> None:
        self._max_entries = max_entries
        self._entries: OrderedDict[tuple[str, str], datetime | None] = OrderedDict()

    def ack(self, channel_id: str, message_id: str, at: datetime | None) -> None:
        key = (channel_id, message_id)
        self._entries[key] = at
        self._entries.move_to_end(key)
        while len(self._entries) > self._max_entries:
            self._entries.popitem(last=False)

    def contains(self, channel_id: str, message_id: str) -> bool:
        return (channel_id, message_id) in self._entries

    def take(self, channel_id: str, message_id: str) -> datetime | None:
        return self._entries.pop((channel_id, message_id), None)


ack_board = AckBoard()


def ack(channel_id: str, message_id: str, at: datetime | None) -> None:
    ack_board.ack(channel_id, message_id, at)


class ProactiveSource(str, Enum):
    TASK_NOTICE = 'task_notice'
    COMPLETION = 'completion'
    PROGRESS = 'progress'
    GATE = 'gate'
    GATE_REMINDER = 'gate_reminder'
    SUBSCRIPTION = 'subscription'
    INTENT = 'intent'
    SCHEDULE = 'schedule'
    ADVISOR_ALERT = 'advisor_alert'
    AUTONOMOUS = 'autonomous'
    USER_REMINDER = 'user_reminder'
    MORNING_BRIEF = 'morning_brief'  # the digest itself: sent uncounted, never deferred


@dataclass(frozen=True)
class ProactiveRef:
    source: ProactiveSource
    source_ref: str | None = None
    manager_id: str | None = None
    dedupe_key: str | None = None


PENDING_MAX = 2000
_pending: set[asyncio.Task[None]] = set()
_last_warning = float('-inf')


def _warn() -> None:
    global _last_warning
    now = time.monotonic()
    if now - _last_warning >= 60:
        _last_warning = now
        logger.warning('metrics write failed or capacity exceeded; observation dropped', exc_info=True)


def background(work: Coroutine[Any, Any, Any]) -> asyncio.Task[None] | None:
    """Bound writes and their duration; return the task for callers retaining a session lock after delivery."""
    if len(_pending) >= PENDING_MAX:
        work.close()
        _warn()
        return

    async def write() -> None:
        try:
            await asyncio.wait_for(work, timeout=10)
        except Exception:
            _warn()

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        work.close()
        _warn()
        return
    task = loop.create_task(write())
    _pending.add(task)
    task.add_done_callback(_pending.discard)
    return task


def record_proactive(ref: ProactiveRef, *, channel_id: str, chat: str, group: bool,
                     external_id: str | None, outcome: Any, dedupe_key: str | None = None) -> None:
    if not chat_settings.metrics_enabled():
        return
    from ..db import proactive_deliveries

    background(proactive_deliveries.insert({
        'source': ref.source.value, 'source_ref': ref.source_ref, 'manager_id': ref.manager_id,
        'channel_id': channel_id, 'chat': chat, 'chat_kind': 'group' if group else 'direct', 'external_id': external_id,
        'dedupe_key': ref.dedupe_key or dedupe_key,
        'outcome': getattr(outcome, 'value', outcome), 'sent_at': datetime.now(timezone.utc),
    }))


def proactive_kwargs(source: ProactiveSource, source_ref: str | None = None,
                     manager_id: str | None = None, *, dedupe_key: str | None = None) -> dict[str, ProactiveRef]:
    return {'proactive': ProactiveRef(source, source_ref, manager_id, dedupe_key)} if chat_settings.metrics_enabled() else {}
