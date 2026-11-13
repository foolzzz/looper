"""One SQLite owner thread per inbox, with ordered synchronous and async callers."""

from __future__ import annotations

import asyncio
from concurrent.futures import Future, ThreadPoolExecutor
from functools import partial, wraps
import threading
from typing import Any, Callable, Self, TypeVar

T = TypeVar("T")


def inbox_thread(method: Callable[..., T]) -> Callable[..., T]:
    @wraps(method)
    def serialized(self: SQLiteInbox, *args: Any, **kwargs: Any) -> T:
        if threading.get_ident() == self._owner:
            return method(self, *args, **kwargs)
        return self.submit(method, self, *args, **kwargs).result()

    return serialized


class SQLiteInbox:
    """Async callers use open/run/aclose; sync maintenance uses the same owner thread."""

    def _initialize(self, initialize: Callable[[], None]) -> None:
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="channel-inbox")
        self._owner: int | None = None
        self._closed = False
        try:
            def open_database() -> None:
                try:
                    initialize()
                except BaseException:
                    if hasattr(self, "_db"):
                        self._db.close()
                    raise

            self.submit(open_database).result()
        except BaseException:
            self._executor.shutdown(wait=True)
            raise

    def submit(self, operation: Callable[..., T], *args: Any, **kwargs: Any) -> Future[T]:
        def execute() -> T:
            self._owner = threading.get_ident()
            return operation(*args, **kwargs)

        return self._executor.submit(execute)

    async def run(self, operation: Callable[..., T], *args: Any, **kwargs: Any) -> T:
        return await asyncio.wrap_future(self.submit(operation, *args, **kwargs))

    @classmethod
    async def open(cls, *args: Any, **kwargs: Any) -> Self:
        opening = asyncio.create_task(asyncio.to_thread(partial(cls, *args, **kwargs)))
        try:
            return await asyncio.shield(opening)
        except asyncio.CancelledError:
            inbox = await opening
            await inbox.aclose()
            raise

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self.submit(self._db.close).result()
        finally:
            self._executor.shutdown(wait=True)

    async def aclose(self) -> None:
        await asyncio.to_thread(self.close)
