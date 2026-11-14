"""One SQLite connection and serial I/O thread per durable channel inbox."""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from functools import partial, wraps
from pathlib import Path
from typing import Awaitable, Callable, Concatenate, ParamSpec, TypeVar

P = ParamSpec("P")
T = TypeVar("T")
S = TypeVar("S", bound="SQLiteInbox")


def serialized(method: Callable[Concatenate[S, P], T]) -> Callable[Concatenate[S, P], Awaitable[T]]:
    # Submit eagerly so a synchronous stop-signal hook can queue its checkpoint
    # before a later polling response, then let async shutdown await that future.
    @wraps(method)
    def call(self: S, *args: P.args, **kwargs: P.kwargs) -> Awaitable[T]:
        return asyncio.get_running_loop().run_in_executor(
            self._executor, partial(method, self, *args, **kwargs))
    return call


class SQLiteInbox:
    def __init__(self) -> None:
        super().__init__()
        self._closed = False
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="channel-inbox")

    @classmethod
    async def open(cls: type[S], path: Path) -> S:
        inbox = cls()
        try:
            await asyncio.get_running_loop().run_in_executor(inbox._executor, inbox._open, path)
        except BaseException:
            await inbox.close()
            raise
        return inbox

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            # Also drains writes whose callers were cancelled. Connection affinity
            # applies to close as well as queries and commits.
            await asyncio.shield(asyncio.get_running_loop().run_in_executor(self._executor, self._close))
        finally:
            self._executor.shutdown(wait=False)

    def _close(self) -> None:
        if db := getattr(self, "_db", None):
            db.close()
