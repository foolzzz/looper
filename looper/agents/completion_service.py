"""The completion service: it hands each chat's waiting completion events to its Manager once the chat's gathering
window closes, and sends the notices of the events that cannot wait any longer (manager completion ledger RFC,
section 3.4).

Every ``TICK_SECONDS`` (and at once when an event is recorded, ``completion_events.poke``), one pass:

- a pending event that waited ``QUEUE_SECONDS`` for its turn (its Manager is held by a long one) falls back
  (``queue_deadline``) and its notice goes;
- every code notice due is sent through the one claim (``store.claim_deliveries``): a fallback's, and one its channel
  refused whose backoff is up; one that may have reached the chat (``uncertain``) is never sent again;
- a notice or a completion reply left ``sending`` for ``SENDING_STALE_SECONDS`` (its send never answered, or its
  record could not be written) becomes ``uncertain``, and a reply's events ``delivered``: never sent again, and the
  chat's next turn may start;
- a chat whose oldest pending event is due (``WINDOW_SECONDS`` after it was recorded) and that no turn is telling
  gets a ``COMPLETION_DUE`` on its Manager's queue, at most once per ``RESEND_SECONDS`` (a duplicate claims
  nothing); a Manager that does not run gets the chat's notices instead (``manager_off``).

When it starts, the ``running`` events of Managers that do not run are settled (``restart``; a reply that was being
sent is ``uncertain``), and a notice left ``sending`` by the server before is ``uncertain``: a running Manager
recovered its own events when it started. One service runs per ``looper serve``.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from typing import Any

from ..db import manager_events as store
from . import completion_events
from .completion_events import Target
from .drain import DrainState
from .message import manager_address
from .supervisor import SERVICE_STOP_AGAIN_SEC, cancel_tasks

logger = logging.getLogger(__name__)

TICK_SECONDS = 5.0
RESEND_SECONDS = 30.0
# How often a pass reads the counts ``system.health`` shows (it reads them from memory, never the database).
HEALTH_SECONDS = 30.0
_HEALTH_KEYS = ("pending", "oldest_pending_seconds", "window_hours", "by_state", "by_reason", "turn_cost_usd",
                "failing", "cost_status", "unpriced_runs")
_running: CompletionService | None = None


def health() -> dict[str, Any] | None:
    """The running service's latest counts (``store.summary``, read at most every ``HEALTH_SECONDS``), with when;
    None before its first read, or when none runs."""
    return dict(_running._health) if _running is not None and _running._health is not None else None


class CompletionService:
    def __init__(self, *, bus: Any, manager_running: Callable[[str], bool], running_managers: Callable[[], list[str]],
                 interval: float = TICK_SECONDS, drain: DrainState | None = None,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self._bus = bus
        self._manager_running = manager_running
        self._running_managers = running_managers
        self._interval = interval
        self._drain = drain
        self._clock = clock
        self._woken = asyncio.Event()
        self._sent: dict[tuple[str, str, str], float] = {}  # when each chat's wake-up was last queued
        self._task: asyncio.Task[Any] | None = None
        self._health: dict[str, Any] | None = None
        self._health_at = float("-inf")
        # Built before any Manager of this server starts: a notice ``sending`` since earlier was this process's
        # predecessor's (``store.settle_stale``).
        self._born = datetime.now(timezone.utc)

    def poke(self) -> None:
        self._woken.set()

    async def start(self) -> None:
        if self._task is not None:
            return
        try:
            if moved := await store.recover_stopped(self._running_managers()):
                logger.info("completion service: %d event(s) of Managers that do not run fell back (restart)", moved)
            for row in await store.settle_stale(before=self._born):
                logger.warning("manager_event %s: its %s was being sent when the server stopped: uncertain, not "
                               "sent again", row["id"], "reply" if row["state"] == "delivered" else "notice")
        except Exception:
            logger.exception("completion service: could not recover the events of the server before it")
        global _running
        completion_events.set_poke(self.poke)
        _running = self
        self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        global _running
        completion_events.set_poke(None)
        if _running is self:
            _running = None
        task, self._task = self._task, None
        # One second, not the process-reap grace: this loop only sleeps, and shutdown has 15s for every stop.
        await cancel_tasks([task], again_after=SERVICE_STOP_AGAIN_SEC)

    async def _loop(self) -> None:
        while True:
            self._woken.clear()
            try:
                await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning("completion service: a pass failed; trying again at the next tick", exc_info=True)
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._woken.wait(), self._interval)
            await asyncio.sleep(0.5)  # an end recorded with others: one pass for the burst

    async def run_once(self, *, now: datetime | None = None) -> int:
        """One pass at ``now`` (the wall clock by default); how many chats it woke or fell back."""
        if self._drain is not None and self._drain.draining:
            return 0
        now = now or datetime.now(timezone.utc)
        # A notice or a reply whose send never answered (or whose record was not written) may have gone: never sent
        # again (from the wall clock: a test's ``now`` must not age a send under way).
        stale = datetime.now(timezone.utc) - timedelta(seconds=store.SENDING_STALE_SECONDS)
        for row in await store.settle_stale(before=stale):
            logger.warning("manager_event %s: its %s never finished sending: uncertain, not sent again", row["id"],
                           "reply" if row["state"] == "delivered" else "notice")
        for row in await store.expire(now):
            logger.info("completion fallback id=%s reason=queue_deadline", row["id"])
        # Every notice due (pending, or refused and its backoff up), through the one claim.
        await completion_events.deliver_notices(self._bus, now=now)
        moved = 0
        monotonic = self._clock()
        for key in await store.due_targets(now):
            target = Target(*key)
            source = manager_address(target.manager_id)
            if not self._manager_running(target.manager_id):
                await completion_events.fall_back(self._bus, source, reason="manager_off", target=target)
                moved += 1
                continue
            if monotonic - self._sent.get(key, float("-inf")) < RESEND_SECONDS:
                continue
            self._sent[key] = monotonic
            await self._bus.publish(completion_events.wake(source, target))
            moved += 1
        for key in [key for key, at in self._sent.items() if monotonic - at >= RESEND_SECONDS]:
            del self._sent[key]
        if monotonic - self._health_at >= HEALTH_SECONDS:
            found = await store.summary()
            self._health = {**{key: found[key] for key in _HEALTH_KEYS}, "checked_at": now.isoformat()}
            self._health_at = monotonic
        return moved
