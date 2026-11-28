"""Registers, every few minutes, what the ledger's hooks missed (completion-ledger RFC 4.5).

It does nothing while ``ledger.enabled`` is off (one setting read per tick). The first tick after the ledger is turned
on reconciles at once, which is the catch-up for what was open before; after that it runs every ``RECONCILE_SECONDS``.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from ..db import ledger
from .supervisor import cancel_tasks

logger = logging.getLogger(__name__)

TICK_SECONDS = 30.0


class LedgerReconciler:
    def __init__(self, *, tick: float = TICK_SECONDS, interval: float = ledger.RECONCILE_SECONDS) -> None:
        self._tick, self._interval = tick, interval
        self._task: asyncio.Task[Any] | None = None
        self._last: float | None = None  # when it last ran; None while the ledger was off at the last tick

    async def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._loop(), name="ledger-reconciler")

    async def stop(self) -> None:
        task, self._task = self._task, None
        await cancel_tasks([task])

    async def run_once(self, now: float | None = None) -> int | None:
        """Reconcile when the ledger is on and it is time; how many it registered, None when it did not run."""
        now = time.monotonic() if now is None else now
        if not await ledger.enabled():
            self._last = None
            return None
        if self._last is not None and now - self._last < self._interval:
            return None
        self._last = now
        return await ledger.reconcile()

    async def _loop(self) -> None:
        while True:
            try:
                await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning("the ledger reconciliation failed; trying again later", exc_info=True)
            await asyncio.sleep(self._tick)
