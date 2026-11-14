"""Diagnose event-loop blocks independently of the event loop itself."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import sys
import threading
import time
import traceback
from collections import deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from types import FrameType

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class LoopSummary:
    loop_stalls: int = 0
    loop_max_stall_ms: int = 0
    process_stalls: int = 0


class LoopWatch:
    """A heartbeat task and a daemon observer; start and stop on the loop thread."""

    _INTERVAL = 0.25
    _STACK_AT = (1.0, 5.0, 15.0)

    def __init__(self, *, clock: Callable[[], float] = time.monotonic,
                 frames: Callable[[], Mapping[int, FrameType]] = sys._current_frames,
                 log: Callable[..., None] = logger.warning) -> None:
        self._clock, self._frames, self._log = clock, frames, log
        self._stalls: deque[tuple[float, float]] = deque(maxlen=128)
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._task: asyncio.Task[None] | None = None
        self._loop_thread_id: int | None = None
        self._beat = self._last_wake = self._process_resumed = 0.0
        self._observed_beat = 0.0
        self._stall_start: float | None = None
        self._stack_stage = 0
        self._last_frame: tuple[object, int] | None = None
        self._loop_stalls = self._loop_max_stall_ms = self._process_stalls = 0

    def start(self) -> None:
        if self._thread is not None:
            raise RuntimeError("the event-loop watchdog has already started")
        loop = asyncio.get_running_loop()
        self._loop_thread_id = threading.get_ident()
        self._beat = self._observed_beat = self._last_wake = self._clock()
        self._task = loop.create_task(self._heartbeat(), name="looper-loop-heartbeat")
        self._thread = threading.Thread(target=self._watch, name="looper-loop-watch", daemon=True)
        self._thread.start()

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=0.5)

    async def _heartbeat(self) -> None:
        while True:
            now = self._clock()
            if now - self._beat > 1.0:
                with self._lock:
                    self._stalls.append((self._beat, now))
            self._beat = now
            await asyncio.sleep(self._INTERVAL)

    def _watch(self) -> None:
        while not self._stop.wait(self._INTERVAL):
            self._check()

    def _check(self) -> None:
        now = self._clock()
        oversleep = now - self._last_wake - self._INTERVAL
        self._last_wake = now
        beat = self._beat
        if oversleep > 1.0:
            with self._lock:
                self._process_stalls += 1
                self._stalls.append((now - oversleep, now))
            self._log("the server process was not scheduled for %.1f s "
                      "(machine overloaded or swapping); not an event-loop block", oversleep)
            # A stale heartbeat cannot identify which thread was blocked while
            # neither thread ran. Diagnose only time after the process resumed.
            self._process_resumed = now
            self._observed_beat = beat
            self._clear_stall()
            return

        if beat != self._observed_beat:
            if self._stall_start is not None:
                duration = beat - self._stall_start
                self._record_max(duration)
                self._log("event loop was blocked for %.1f s", duration)
                self._clear_stall()
            self._observed_beat = beat

        began = max(beat, self._process_resumed)
        duration = now - began
        if duration <= 1.0:
            return
        if self._stall_start is None:
            self._stall_start = began
            with self._lock:
                self._loop_stalls += 1
        self._record_max(duration)
        if self._stack_stage >= len(self._STACK_AT) or duration < self._STACK_AT[self._stack_stage]:
            return
        self._stack_stage += 1
        frame = self._frames().get(self._loop_thread_id)
        try:
            identity = (frame.f_code, frame.f_lineno) if frame is not None else None
            if self._stack_stage > 1 and identity == self._last_frame:
                return
            self._last_frame = identity
            stack = ("".join(traceback.format_stack(frame, limit=40)) if frame is not None
                     else "<loop thread stack unavailable>")
            self._log("event loop blocked for %.1f s so far; loop thread stack:\n%s", duration, stack)
        finally:
            del frame  # Do not retain a running thread's frames between samples.

    def _clear_stall(self) -> None:
        self._stall_start = None
        self._stack_stage = 0
        self._last_frame = None

    def _record_max(self, duration: float) -> None:
        with self._lock:
            self._loop_max_stall_ms = max(self._loop_max_stall_ms, int(duration * 1000))

    def stall_seconds_between(self, started: float, ended: float) -> float:
        """Largest overlapping stall, independent of summary consumption.

        Include stale heartbeats/observer wakes: a query can resume before
        either watchdog callback after the whole process was suspended.
        """
        with self._lock:
            intervals = [*self._stalls, (self._beat, ended), (self._last_wake, ended)]
        return max((max(0.0, min(end, ended) - max(begin, started))
                    for begin, end in intervals if end - begin > 1.0), default=0.0)

    def take_summary(self) -> LoopSummary:
        with self._lock:
            result = LoopSummary(self._loop_stalls, self._loop_max_stall_ms, self._process_stalls)
            self._loop_stalls = self._loop_max_stall_ms = self._process_stalls = 0
            return result
