"""Process-local admission barrier for a planned restart; nothing is persisted."""

from __future__ import annotations

import asyncio
import logging
import math
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from enum import StrEnum
from typing import Any

logger = logging.getLogger(__name__)


class DrainPhase(StrEnum):
    SERVING = "serving"
    DRAINING = "draining"
    DRAINED = "drained"


class DrainScope(StrEnum):
    TURNS = "turns"
    RUNS = "runs"


@dataclass(frozen=True)
class DrainStatus:
    phase: DrainPhase = DrainPhase.SERVING
    scope: DrainScope | None = None
    since: datetime | None = None
    deadline: datetime | None = None


class DrainState:
    def __init__(self, *, clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc)) -> None:
        self._clock = clock
        self._status = DrainStatus()
        self._serving = asyncio.Event()
        self._serving.set()
        self._wakers: list[Callable[[], None]] = []
        self._sources: list[Callable[[], Mapping[str, Any]]] = []
        self._poll_timeout: float | None = None
        self._poll_timer: asyncio.TimerHandle | None = None

    @property
    def draining(self) -> bool:
        return self._status.phase != DrainPhase.SERVING

    def add_waker(self, callback: Callable[[], None]) -> None:
        self._wakers.append(callback)

    def add_source(self, callback: Callable[[], Mapping[str, Any]]) -> None:
        self._sources.append(callback)

    async def wait_serving(self) -> None:
        while self.draining:
            await self._serving.wait()

    def begin(self, scope: DrainScope, timeout_seconds: float | None = None, *,
              poll_timeout_seconds: float | None = None) -> dict[str, Any]:
        scope = DrainScope(scope)
        timeout = (180 if scope == DrainScope.TURNS else 1200) if timeout_seconds is None else timeout_seconds
        if isinstance(timeout, bool) or not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("timeout_seconds must be a positive finite number")
        now = self._clock()
        try:
            deadline = now + timedelta(seconds=timeout)
        except OverflowError:
            raise ValueError("timeout_seconds exceeds the supported deadline range") from None
        self._clear_poll_timer()
        self._poll_timeout = poll_timeout_seconds
        self._serving.clear()
        self._status = DrainStatus(DrainPhase.DRAINING, scope, now, deadline)
        logger.info("drain phase=draining scope=%s deadline=%s", scope.value, deadline.isoformat())
        return self.poll()

    def _clear_poll_timer(self) -> None:
        if self._poll_timer is not None:
            self._poll_timer.cancel()
            self._poll_timer = None

    def poll(self) -> dict[str, Any]:
        """Renew the controller lease, independently of its deadline to stop the process."""
        self._clear_poll_timer()
        if self.draining and self._poll_timeout is not None:
            self._poll_timer = asyncio.get_running_loop().call_later(self._poll_timeout, self._poll_lost)
        return self.snapshot()

    def _poll_lost(self) -> None:
        logger.warning("drain controller polls stopped; resuming admission")
        self.cancel()

    def cancel(self) -> dict[str, Any]:
        self._clear_poll_timer()
        self._poll_timeout = None
        self._status = DrainStatus()
        self._serving.set()
        for wake in self._wakers:
            wake()
        logger.info("drain phase=serving")
        return self.snapshot()

    def snapshot(self) -> dict[str, Any]:
        waiting: dict[str, Any] = {"runs": [], "manager_turns": [], "planner_turns": [],
                                   "gates": 0, "waits": 0, "consults": 0, "starting": 0}
        for source in self._sources:
            for key, value in source().items():
                if isinstance(waiting[key], list):
                    waiting[key].extend(value)
                else:
                    waiting[key] += value
        if self.draining:
            # S0 runs are never keepable. S1 may leave keepable runs alive for a turns drain.
            blocking_runs = any(self._status.scope == DrainScope.RUNS or not run["keepable"]
                                for run in waiting["runs"])
            blocked = blocking_runs or any(value for key, value in waiting.items() if key != "runs")
            phase = DrainPhase.DRAINING if blocked else DrainPhase.DRAINED
            if phase != self._status.phase:
                logger.info("drain phase=%s waiting=%s", phase.value, waiting)
                self._status = replace(self._status, phase=phase)
        status = self._status
        return {"phase": status.phase.value, "scope": status.scope.value if status.scope else None,
                "since": status.since.isoformat() if status.since else None,
                "deadline": status.deadline.isoformat() if status.deadline else None,
                "deadline_passed": bool(status.deadline and self._clock() >= status.deadline), "waiting": waiting}
