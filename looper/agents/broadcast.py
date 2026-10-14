"""In-process per-task SSE broadcaster.

Modeled after ARTEX's broadcast.go: each task gets a set of subscriber
queues. Publishing is non-blocking — if a subscriber's buffer is full the
event is dropped and the client catches up from the DB on reconnect.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

logger = logging.getLogger(__name__)

_BUFFER_SIZE = 256


class Broadcaster:
    """Fan-out activity events to SSE subscribers, keyed by task_id."""

    def __init__(self) -> None:
        self._subs: dict[str, set[asyncio.Queue[dict[str, Any] | None]]] = {}

    def subscribe(self, task_id: str) -> asyncio.Queue[dict[str, Any] | None]:
        q: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue(maxsize=_BUFFER_SIZE)
        self._subs.setdefault(task_id, set()).add(q)
        return q

    def unsubscribe(self, task_id: str, q: asyncio.Queue[dict[str, Any] | None]) -> None:
        subs = self._subs.get(task_id)
        if subs:
            subs.discard(q)
            if not subs:
                del self._subs[task_id]

    def publish(self, task_id: str, event: dict[str, Any]) -> None:
        subs = self._subs.get(task_id)
        if not subs:
            return
        for q in list(subs):
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                logger.debug("dropping event for slow subscriber on task %s", task_id)

    def close_task(self, task_id: str) -> None:
        """Signal all subscribers for a task that the stream is done."""
        subs = self._subs.pop(task_id, None)
        if subs:
            for q in subs:
                try:
                    q.put_nowait(None)
                except asyncio.QueueFull:
                    pass
