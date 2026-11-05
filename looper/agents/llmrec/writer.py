"""A bounded, retrying writer, independent of the forwarding path."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from looper.db import llm_requests

if TYPE_CHECKING:
    import asyncpg
    from .capture import Capture

logger = logging.getLogger(__name__)


class DbSink:
    def __init__(self, pool: asyncpg.Pool | None, *, max_pending_bytes: int = 256 * 1024 * 1024):
        self.pool = pool
        self.max_pending_bytes = max_pending_bytes
        self.pending = 0
        self.recorded = 0
        self.write_errors = 0
        self._pending_bytes = 0
        self._queue: asyncio.Queue[tuple[Capture, int]] = asyncio.Queue()
        self._closing = asyncio.Event()
        self._task: asyncio.Task | None = None

    def offer(self, capture: Capture) -> bool:
        size = capture.size()
        if self._closing.is_set() or self._pending_bytes + size > self.max_pending_bytes:
            return False
        self._pending_bytes += size
        self.pending += 1
        self._queue.put_nowait((capture, size))
        return True

    async def run(self) -> None:
        self._task = asyncio.current_task()
        batch: list[tuple[Capture, int]] = []
        backoff = 1
        try:
            while not self._closing.is_set() or self.pending:
                if not batch:
                    try:
                        batch.append(await asyncio.wait_for(self._queue.get(), .5))
                    except TimeoutError:
                        continue
                    deadline = asyncio.get_running_loop().time() + .5
                    while len(batch) < 50:
                        remaining = deadline - asyncio.get_running_loop().time()
                        if remaining <= 0:
                            break
                        try:
                            if self._closing.is_set():
                                batch.append(self._queue.get_nowait())
                            else:
                                batch.append(await asyncio.wait_for(self._queue.get(), remaining))
                        except (TimeoutError, asyncio.QueueEmpty):
                            break
                try:
                    if self.pool is None:
                        raise ConnectionError("recording database is not connected")
                    await llm_requests.write_batch([capture for capture, _ in batch], pool=self.pool)
                except Exception:
                    self.write_errors += 1
                    # Database exceptions can contain DSNs or query parameters. Log metadata only.
                    logger.warning("model request write failed; pending=%d retry_s=%d", self.pending, backoff)
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 2, 30)
                    continue
                self.recorded += len(batch)
                self.pending -= len(batch)
                self._pending_bytes -= sum(size for _, size in batch)
                batch.clear()
                backoff = 1
        finally:
            self._task = None

    async def close(self, timeout: float = 10) -> None:
        self._closing.set()
        task = self._task
        if task is not None and task is not asyncio.current_task():
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout)
            except TimeoutError:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
