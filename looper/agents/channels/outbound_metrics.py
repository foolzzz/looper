"""Service-local outbound events and the operator's 24-hour measurements."""
from __future__ import annotations

import time
from collections import Counter, deque
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import StrEnum

from ...db import channel_outbox

WINDOW_SECONDS = 86400
MAX_EVENTS = 10000


class EventKind(StrEnum):
    BACKOFF = "backoff"
    BREAKER_OPEN = "breaker_open"
    BREAKER_HALF_OPEN = "breaker_half_open"
    BREAKER_RECOVERED = "breaker_recovered"


@dataclass(frozen=True)
class Event:
    channel_id: str
    kind: EventKind
    at: float


class EventBuffer:
    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self._events: deque[Event] = deque(maxlen=MAX_EVENTS)

    def _prune(self):
        cutoff = self.clock() - WINDOW_SECONDS
        while self._events and self._events[0].at < cutoff:
            self._events.popleft()

    def record(self, channel_id: str, kind: EventKind, *, at: float | None = None):
        self._prune()
        self._events.append(Event(channel_id, EventKind(kind), self.clock() if at is None else at))

    def snapshot(self) -> dict[str, Counter]:
        self._prune()
        result: dict[str, Counter] = {}
        for event in self._events:
            result.setdefault(event.channel_id, Counter())[event.kind] += 1
        return result

    def counts(self, channel_id: str) -> Counter:
        return self.snapshot().get(channel_id, Counter())


# Shared across adapter restarts; released only with the service process. Contains no message content.
events = EventBuffer()


async def summary(server, db):
    from .. import chat_settings

    if not chat_settings.outbound_enabled():
        return {"enabled": False}
    now = datetime.now(timezone.utc)
    counters = events.snapshot()
    names = {row["channel_id"]: row["display_name"] for row in await db.fetch(
        "SELECT channel_id, display_name FROM channel_configs")}
    stored = await channel_outbox.metric_counts(now - timedelta(seconds=WINDOW_SECONDS), pool=db)
    rows = []
    for channel_id in sorted(names.keys() | stored.keys() | counters.keys()):
        counts = stored.get(channel_id, {})
        recent = counters.get(channel_id, Counter())
        adapter = server._channels.get(channel_id) if server._channels else None
        gateway = getattr(adapter, "_gateway", None)
        rows.append({"channel_id": channel_id, "name": names.get(channel_id) or channel_id,
            "queued": counts.get("queued", 0), "backoffs_24h": recent[EventKind.BACKOFF],
            "breaker_opens_24h": recent[EventKind.BREAKER_OPEN],
            "failed_24h": counts.get("failed_24h", {}), "uncertain_24h": counts.get("uncertain_24h", 0),
            "breaker_state": gateway.breaker.state if gateway else None})
    return rows
