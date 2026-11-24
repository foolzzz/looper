"""Consume durable journal ranges after terminal publication, independently of the Planner and vault writer."""
from __future__ import annotations

import asyncio
import contextlib
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..db import connection, memory_retro as store
from . import memory_lessons, memory_lessons_model, memory_retro, memory_retro_role, model_call
from .spawn import SpawnParent

logger = logging.getLogger(__name__)
TICK_SECONDS = 10
CALL_SECONDS = 110
LEASE_SECONDS = 140


def terminal_listener(scheduled, service):
    """Both callbacks run after teardown; either failure leaves the other callback available."""
    async def ended(task_id: str, status: str) -> None:
        for callback in (scheduled, service.enqueue_finished):
            try:
                await callback(task_id, status)
            except Exception:
                logger.warning("task terminal listener deferred task=%s", task_id)
    return ended


class MemoryRetroService:
    def __init__(self, runtime_root: Path, *, db: Any = None, drain: Any = None,
                 claude_bin: str = "claude", backend: Any = None, sender: Any = None,
                 clock=None):
        self.runtime_root, self.db, self.drain = runtime_root, db, drain
        self.claude_bin, self.backend, self.sender = claude_bin, backend, sender
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self._task: asyncio.Task | None = None
        self._woken = asyncio.Event()
        self._pass_lock = asyncio.Lock()
        self._busy = False
        self._stopping = False

    def wake(self) -> None:
        self._woken.set()

    def drain_waiting(self) -> int:
        return int(self._busy)

    async def start(self, database_url: str | None = None) -> None:
        if self._task is None:
            self._stopping = False
            self._task = asyncio.create_task(self._loop(), name="memory-retro")

    async def stop(self) -> None:
        self._stopping = True
        self.wake()
        if self._task is not None:
            await asyncio.shield(self._task)
            self._task = None

    async def _settings(self):
        view = await memory_lessons.settings_view(self.db)
        return memory_lessons_model.parse_settings(view["settings"]) if view["enabled"] and not view["problem"] else None

    async def enqueue_finished(self, task_id: str, status: str) -> None:
        if status not in ("completed", "failed") or (settings := await self._settings()) is None:
            return
        async with (self.db or connection.get_pool()).acquire() as conn:
            await store.enqueue_finished(conn, task_id, status, settings=settings, now=self.clock())
        self.wake()

    async def _loop(self) -> None:
        while not self._stopping:
            self._woken.clear()
            try:
                processed = await self.run_once()
            except Exception:
                # Private inputs and model output never enter service logs.
                logger.warning("memory retrospective pass deferred")
                processed = False
            if not processed and not self._stopping:
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self._woken.wait(), TICK_SECONDS)

    async def run_once(self) -> bool:
        async with self._pass_lock:
            if (settings := await self._settings()) is None or (self.drain is not None and self.drain.draining):
                return False
            self._busy = True
            try:
                return await self._pass(settings)
            finally:
                self._busy = False

    async def _pass(self, settings) -> bool:
        pool = self.db or connection.get_pool()
        async with pool.acquire() as conn:
            await store.bootstrap(conn, now=self.clock())
            await store.recover_finished(conn, settings=settings, now=self.clock())
            run = await store.claim_next(conn, settings=settings, now=self.clock(), lease_seconds=LEASE_SECONDS)
            if run is None:
                notice = await store.claim_failure_notice(conn, now=self.clock())
        if run is None:
            await self._failure_notice(settings, notice)
            return False
        done = None
        try:
            async with asyncio.timeout(CALL_SECONDS):
                async with pool.acquire() as conn:
                    data = await store.load_input(conn, run, settings=settings, now=self.clock())
                prepared = memory_retro.assemble_input(**data, today=self.clock().astimezone().date(),
                                                       project_cap=settings.project_cap)
                done = await memory_retro_role.run(prepared, parent=SpawnParent(run["task_id"]),
                    runtime_root=self.runtime_root, cap_usd=float(run["budget_usd"]),
                    claude_bin=self.claude_bin, backend=self.backend)
        except TimeoutError:
            done = model_call.CallFailed(execution=model_call.Execution(None), failure=model_call.CallFailure.TIMEOUT,
                                         reason="retro deadline")
        except Exception:
            done = model_call.CallFailed(execution=model_call.Execution(None), failure=model_call.CallFailure.RUNTIME,
                                         reason="retro unavailable")
        async with pool.acquire() as conn:
            if isinstance(done, model_call.CallFailed):
                await store.settle_attempt(conn, run["id"], run["lease_token"], cost_usd=done.cost_usd,
                    record_id=done.record_id, failure=done.failure.value, now=self.clock())
            else:
                try:
                    await store.apply_reply(conn, run["id"], run["lease_token"], done.value, prepared,
                        settings=settings, now=self.clock(), cost_usd=done.cost_usd, record_id=done.record_id)
                except Exception:
                    await store.settle_attempt(conn, run["id"], run["lease_token"], cost_usd=done.cost_usd,
                        record_id=done.record_id, failure="apply", now=self.clock())
            notice = await store.claim_failure_notice(conn, now=self.clock())
        await self._failure_notice(settings, notice)
        return True

    async def _failure_notice(self, settings, notice) -> None:
        if notice:
            logger.warning("memory retrospectives failed for three consecutive tasks")
            if self.sender is not None and settings.weekly.push_channel:
                from .operator_identity import operator_sender_id
                from ..db import system_settings
                chat = await operator_sender_id(settings.weekly.push_channel)
                day = self.clock().astimezone().date().isoformat()
                if chat and await system_settings.claim("memory_retro_failure_notice", day, pool=self.db):
                    await self.sender(settings.weekly.push_channel, chat,
                                      "连续三个任务的经验复盘失败，请检查任务动态与角色设置。")
