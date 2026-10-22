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
from typing import Any, TypeVar

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
            await abandon(task)
            if asked.done():
                raise TurnCancelled()
            raise TimeoutError()
        except asyncio.CancelledError:
            await abandon(task)
            raise
        finally:
            unregister()
            if not asked.done():
                asked.cancel()


# How long what a turn gave up on is waited for, once cancelled, and then its undo. A cancelled database call may
# never end: asyncpg hands its connection back to the pool behind a shield, and resetting it waits on a server the
# network may no longer reach.
ABANDON_SEC = 10.0


async def abandon(task: asyncio.Future[Any], undo: Callable[[], Awaitable[None]] | None = None) -> None:
    """Cancel ``task``, wait for it and run ``undo``, each bounded by ``ABANDON_SEC`` (past it, what still runs is
    cancelled again and left behind); a cancel of the caller that comes meanwhile (``cancel_and_wait`` cancels
    again) is held until both are done, then raised."""
    cancelled = False

    async def bounded(aw: Awaitable[Any]) -> None:
        nonlocal cancelled
        inner = asyncio.ensure_future(aw)
        deadline = asyncio.get_running_loop().time() + ABANDON_SEC
        while not inner.done():
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                logger.warning("an abandoned turn step did not end within %.1f s; left behind", ABANDON_SEC)
                inner.cancel()
                break
            try:
                await asyncio.wait({inner}, timeout=remaining)
            except asyncio.CancelledError:
                cancelled = True
        if inner.done() and not inner.cancelled() and inner.exception() is not None:
            logger.warning("an abandoned turn step failed: %r", inner.exception())

    task.cancel()
    await bounded(task)
    if undo is not None:
        await bounded(undo())
    if cancelled:
        raise asyncio.CancelledError()


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
