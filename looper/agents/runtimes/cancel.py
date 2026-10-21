"""One cancellation primitive for a turn: a synchronous, idempotent request that runtimes subscribe to.

``on_request`` calls the callback at once when the request already came, so a runtime that registers late (before the
process exists, before the first event, after falling back to another path) never misses it.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from typing import TypeVar

logger = logging.getLogger(__name__)

T = TypeVar("T")


class TurnCancelled(Exception):
    """The turn's cancel was requested while waiting."""


class TurnCancel:
    def __init__(self) -> None:
        self._requested = False
        self.requested_at: datetime | None = None
        self._callbacks: list[Callable[[], None]] = []
        self.prompt_sent = False
        self._sent_callbacks: list[Callable[[], None]] = []

    @classmethod
    def never(cls) -> TurnCancel:
        """A fresh instance for callers that pass none: nobody holds it, so it is never requested."""
        return cls()

    @property
    def requested(self) -> bool:
        return self._requested

    def request(self) -> bool:
        if self._requested:
            return False
        self._requested = True
        self.requested_at = datetime.now(timezone.utc)
        _call_all(list(self._callbacks), "cancel")
        return True

    def on_request(self, cb: Callable[[], None]) -> Callable[[], None]:
        return _register(self._callbacks, cb, self._requested, "cancel")

    def sent(self) -> None:
        if self.prompt_sent:
            return
        self.prompt_sent = True
        _call_all(list(self._sent_callbacks), "sent")

    def on_sent(self, cb: Callable[[], None]) -> Callable[[], None]:
        return _register(self._sent_callbacks, cb, self.prompt_sent, "sent")

    async def race(self, aw: Awaitable[T], *, timeout: float | None = None) -> T:
        """Await ``aw``; a request first cancels it and raises ``TurnCancelled``, a timeout raises ``TimeoutError``."""
        if self._requested:
            if inspect.iscoroutine(aw):
                aw.close()
            raise TurnCancelled()
        loop = asyncio.get_running_loop()
        task = asyncio.ensure_future(aw)
        asked: asyncio.Future[None] = loop.create_future()

        def wake() -> None:
            if not asked.done():
                asked.set_result(None)

        unregister = self.on_request(wake)
        try:
            await asyncio.wait({task, asked}, timeout=timeout, return_when=asyncio.FIRST_COMPLETED)
            if task.done():
                return task.result()
            await _cancel(task)
            if asked.done():
                raise TurnCancelled()
            raise TimeoutError()
        except asyncio.CancelledError:
            await _cancel(task)
            raise
        finally:
            unregister()
            if not asked.done():
                asked.cancel()


async def _cancel(task: asyncio.Future[object]) -> None:
    if not task.done():
        task.cancel()
    await asyncio.gather(task, return_exceptions=True)


def _register(
    callbacks: list[Callable[[], None]], cb: Callable[[], None], fired: bool, what: str
) -> Callable[[], None]:
    if fired:
        _call_all([cb], what)
        return lambda: None
    callbacks.append(cb)

    def unregister() -> None:
        try:
            callbacks.remove(cb)
        except ValueError:
            pass

    return unregister


def _call_all(callbacks: list[Callable[[], None]], what: str) -> None:
    for cb in callbacks:
        try:
            cb()
        except Exception:
            logger.exception("turn %s callback failed", what)
