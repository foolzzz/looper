"""A dedicated database session owns the control plane until release or terminal loss."""
from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import socket
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

import asyncpg

from . import proc_tree
from .loop_watch import LoopWatch

logger = logging.getLogger(__name__)

# Two-int advisory keys occupy a different space from the one-int schema lock.
LOCK_KEY = (0x6C6F6F70, 0x63706C6B)
HEALTH_INTERVAL_SECONDS = 5.0
HEALTH_TIMEOUT_SECONDS = 5.0
RECHECK_TIMEOUT_SECONDS = 15.0
RECHECK_WINDOW_SECONDS = 30.0
EXIT_LOCK_LOST = 75
EPOCH_KEY = "control_plane_epoch"

_HOLDER = """
SELECT a.pid, a.application_name, host(a.client_addr) AS client_addr, a.backend_start
FROM pg_locks l JOIN pg_stat_activity a ON a.pid = l.pid
WHERE l.locktype = 'advisory'
  AND l.database = (SELECT oid FROM pg_database WHERE datname = current_database())
  AND l.classid = $1::oid AND l.objid = $2::oid AND l.objsubid = 2 AND l.granted
"""
_HEALTH = """
SELECT EXISTS (SELECT 1 FROM pg_locks WHERE locktype = 'advisory'
  AND pid = pg_backend_pid() AND classid = $1::oid AND objid = $2::oid
  AND objsubid = 2 AND granted)
"""


class LeaseState(StrEnum):
    HELD = "held"
    UNCERTAIN = "uncertain"
    RELEASED = "released"
    LOST = "lost"


@dataclass(frozen=True)
class LockHolder:
    pid: int | None
    application_name: str | None
    client_addr: str | None
    backend_start: datetime | None

    def describe(self) -> str:
        return ", ".join(f"{'backend_pid' if name == 'pid' else name}={value or 'unknown'}"
                         for name, value in vars(self).items())


class LockHeld(RuntimeError):
    def __init__(self, holder: LockHolder):
        self.holder = holder
        super().__init__(holder.describe())


class ControlPlaneLease:
    def __init__(self, conn: asyncpg.Connection, on_lost: Callable[[str], None],
                 *, loop_watch: LoopWatch | None = None):
        self._loop_watch = loop_watch
        self._conn = conn
        self._on_lost = on_lost
        self.state = LeaseState.HELD
        self.epoch: int | None = None
        self._query_lock = asyncio.Lock()
        self._health_task: asyncio.Task | None = None

    @classmethod
    async def acquire(cls, dsn: str, *, on_lost: Callable[[str], None],
                      loop_watch: LoopWatch | None = None) -> ControlPlaneLease:
        name = f"looper serve {os.getpid()}@{socket.gethostname()}"[:63]
        conn = await asyncpg.connect(dsn, server_settings={"application_name": name})
        try:
            if not await conn.fetchval("SELECT pg_try_advisory_lock($1, $2)", *LOCK_KEY):
                row = await conn.fetchrow(_HOLDER, *LOCK_KEY)
                raise LockHeld(LockHolder(**dict(row)) if row else LockHolder(None, None, None, None))
            lease = cls(conn, on_lost, loop_watch=loop_watch)
            conn.add_termination_listener(lambda c: lease.lose("lock connection closed"))
            lease._health_task = asyncio.create_task(lease._health())
            return lease
        except BaseException:
            await conn.close()
            raise

    async def _health(self) -> None:
        deadline: float | None = None
        while self.state in (LeaseState.HELD, LeaseState.UNCERTAIN):
            if self.state is LeaseState.HELD:
                await asyncio.sleep(HEALTH_INTERVAL_SECONDS)
            remaining = deadline - time.monotonic() if deadline is not None else HEALTH_TIMEOUT_SECONDS
            if remaining <= 0:
                self.lose("lock health check remained uncertain for the recheck window")
                return
            timeout = min(RECHECK_TIMEOUT_SECONDS, remaining) if deadline is not None else remaining
            # Contention with epoch setup is not database query time. Bound the
            # recovery window separately so waiting cannot extend uncertainty.
            started, lag = time.monotonic(), 0.0
            try:
                async with asyncio.timeout(remaining if deadline is not None else None):
                    async with self._query_lock:
                        if self.state not in (LeaseState.HELD, LeaseState.UNCERTAIN):
                            return
                        started = time.monotonic()
                        try:
                            async with asyncio.timeout(timeout):
                                held = await self._conn.fetchval(_HEALTH, *LOCK_KEY)
                        finally:
                            ended = time.monotonic()
                            lag = (self._loop_watch.stall_seconds_between(started, ended)
                                   if self._loop_watch is not None else 0.0)
                if self.state not in (LeaseState.HELD, LeaseState.UNCERTAIN):
                    return
                if held is False:
                    self.lose("health check found no lock")
                    return
                if self._conn.is_closed():
                    self.lose("lock connection closed")
                    return
                if lag or ended - started > timeout or (deadline is not None and ended >= deadline):
                    raise TimeoutError
                if self.state is LeaseState.UNCERTAIN:
                    self.state = LeaseState.HELD
                    proc_tree.set_lease_uncertain(False)
                    deadline = None
                    logger.info("control plane lock confirmed held; resumed agent spawns")
            except Exception as exc:
                if self.state not in (LeaseState.HELD, LeaseState.UNCERTAIN):
                    return
                connection_error = isinstance(exc, (asyncpg.PostgresConnectionError, OSError))
                if self._conn.is_closed() or (connection_error and not isinstance(exc, TimeoutError)):
                    self.lose(f"lock health check failed: {type(exc).__name__}")
                    return
                if not isinstance(exc, TimeoutError) and not lag:
                    self.lose(f"lock health check failed: {type(exc).__name__}")
                    return
                if self.state is LeaseState.HELD:
                    self.state = LeaseState.UNCERTAIN
                    proc_tree.set_lease_uncertain(True)
                    deadline = time.monotonic() + RECHECK_WINDOW_SECONDS
                logger.warning("lease health check timed out after %.1f s (loop lag %.1f s); rechecking",
                               time.monotonic() - started, lag)

    async def bump_epoch(self) -> int:
        async with self._query_lock:
            if self.state is not LeaseState.HELD:
                raise RuntimeError("the control plane lock is not held")
            self.epoch = await self._conn.fetchval("""
                INSERT INTO system_settings (key, value) VALUES ($1, '1')
                ON CONFLICT (key) DO UPDATE
                SET value = (system_settings.value::bigint + 1)::text, updated_at = now()
                RETURNING value::bigint
            """, EPOCH_KEY)
            return self.epoch

    def lose(self, reason: str) -> None:
        if self.state not in (LeaseState.HELD, LeaseState.UNCERTAIN):
            return
        self.state = LeaseState.LOST
        logger.error("control plane lock lost reason=%s; exiting without touching runs", reason)
        self._on_lost(reason)

    async def release(self) -> None:
        if self.state in (LeaseState.HELD, LeaseState.UNCERTAIN):
            self.state = LeaseState.RELEASED
        if self._health_task is not None:
            self._health_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._health_task
        try:
            await asyncio.wait_for(self._conn.close(), HEALTH_TIMEOUT_SECONDS)
        except TimeoutError:
            self._conn.terminate()
            logger.warning("control plane lock connection close timed out; terminated")
