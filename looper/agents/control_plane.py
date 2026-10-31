"""A dedicated database session owns the control plane until release or terminal loss."""
from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import socket
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

import asyncpg

logger = logging.getLogger(__name__)

# Two-int advisory keys occupy a different space from the one-int schema lock.
LOCK_KEY = (0x6C6F6F70, 0x63706C6B)
HEALTH_INTERVAL_SECONDS = 5.0
HEALTH_TIMEOUT_SECONDS = 5.0
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
    def __init__(self, conn: asyncpg.Connection, on_lost: Callable[[str], None]):
        self._conn = conn
        self._on_lost = on_lost
        self.state = LeaseState.HELD
        self.epoch: int | None = None
        self._query_lock = asyncio.Lock()
        self._health_task: asyncio.Task | None = None

    @classmethod
    async def acquire(cls, dsn: str, *, on_lost: Callable[[str], None]) -> ControlPlaneLease:
        name = f"looper serve {os.getpid()}@{socket.gethostname()}"[:63]
        conn = await asyncpg.connect(dsn, server_settings={"application_name": name})
        try:
            if not await conn.fetchval("SELECT pg_try_advisory_lock($1, $2)", *LOCK_KEY):
                row = await conn.fetchrow(_HOLDER, *LOCK_KEY)
                raise LockHeld(LockHolder(**dict(row)) if row else LockHolder(None, None, None, None))
            lease = cls(conn, on_lost)
            conn.add_termination_listener(lambda c: lease.lose("lock connection closed"))
            lease._health_task = asyncio.create_task(lease._health())
            return lease
        except BaseException:
            await conn.close()
            raise

    async def _health(self) -> None:
        while self.state is LeaseState.HELD:
            await asyncio.sleep(HEALTH_INTERVAL_SECONDS)
            try:
                async with asyncio.timeout(HEALTH_TIMEOUT_SECONDS):
                    async with self._query_lock:
                        held = await self._conn.fetchval(_HEALTH, *LOCK_KEY)
                if not held:
                    self.lose("health check found no lock")
            except Exception as exc:
                self.lose(f"lock health check failed: {type(exc).__name__}")

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
        if self.state is not LeaseState.HELD:
            return
        self.state = LeaseState.LOST
        logger.error("control plane lock lost reason=%s; exiting without touching runs", reason)
        self._on_lost(reason)

    async def release(self) -> None:
        if self.state is LeaseState.HELD:
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
