"""Typed step recovery and its single versioned hold lifecycle.

Only act calls this service. Timers publish committed recovery events; a Planner
consumes them serially, and every launch passes through ordinary admission again.
"""
from __future__ import annotations

import asyncio
import logging
import random
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import StrEnum
from typing import Any

from .failures import Failure, FailureClass, FailureTyping, SETTING_KEY
from ..db import step_failures as store, system_settings
from ..db.connection import get_pool

logger = logging.getLogger(__name__)


class FailureHandlingError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


class HandlingAction(StrEnum):
    HOLD = "hold"
    PLANNER = "planner"
    BREAKER = "breaker"
    DUPLICATE = "duplicate"
    IGNORED = "ignored"


@dataclass(frozen=True)
class Handling:
    action: HandlingAction
    failure: dict[str, Any] | None = None
    hold: dict[str, Any] | None = None

    def __post_init__(self):
        object.__setattr__(self, "action", HandlingAction(self.action))


_service: FailureHandlingService | None = None


def get_service() -> FailureHandlingService | None:
    return _service


get = get_service


def configure(service: FailureHandlingService | None) -> None:
    global _service
    _service = service


class FailureHandlingService:
    def __init__(self, *, pool=None, publish=None, clock=None, jitter=None, mode=FailureTyping.OFF):
        self.pool = pool
        self.publish = publish
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self._sleep = asyncio.sleep
        self._quota_deadlines: dict[str, datetime] = {}
        self.jitter = jitter or (lambda: random.uniform(.8, 1.2))
        self.mode = FailureTyping(mode)
        self._timers: dict[str, asyncio.Task] = {}
        self._history_tasks: set[str] = set()
        self._cancelled: set[str] = set()
        self._changed = asyncio.Event()
        self._closed = False
        self.recovering = False
        self._mode_lock = asyncio.Lock()

    def known_mode(self) -> FailureTyping:
        return self.mode

    def hold_until(self, failure: Failure, streak: int) -> datetime | None:
        now = self.clock()
        if failure.cls is FailureClass.TRANSIENT:
            return now + timedelta(seconds=(120 if streak <= 1 else 600) * self.jitter())
        if failure.cls is FailureClass.QUOTA:
            reset = failure.resets_at
            if reset is not None and now < reset <= now + timedelta(days=8):
                return reset + timedelta(seconds=60)
            return now + timedelta(minutes=min(60, 5 * 2 ** min(max(streak - 1, 0), 4)))
        return None

    def quota_delay(self, failure: Failure, backoff: int) -> float:
        until = self.hold_until(failure, backoff)
        return max(0, (until - self.clock()).total_seconds()) if until else 0

    async def wait_turn(self, delay_s: float, *, task_id: str | None = None) -> bool:
        if self._closed or self.mode is not FailureTyping.ACT or task_id in self._cancelled:
            return False
        deadline = asyncio.get_running_loop().time() + max(0, delay_s)
        while not self._closed and self.mode is FailureTyping.ACT and task_id not in self._cancelled:
            changed = self._changed
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                return True
            try:
                await asyncio.wait_for(changed.wait(), timeout=remaining)
            except asyncio.TimeoutError:
                return not self._closed and self.mode is FailureTyping.ACT and task_id not in self._cancelled
        return False

    def _wake_waits(self):
        self._changed.set()
        self._changed = asyncio.Event()

    async def _emit(self, task_id, payload):
        if self.publish is not None:
            await self.publish(task_id, payload)

    async def handle_step_result(self, **kwargs) -> Handling:
        if self._closed or self.mode is not FailureTyping.ACT:
            return Handling('ignored')
        try:
            handled = await store.record_failure(**kwargs, hold_until=lambda streak: self.hold_until(kwargs['failure'], streak),
                                                 pool=self.pool)
        except store.StoreError as exc:
            if exc.code in ('conflict', 'not_found'):
                logger.info('Ignoring non-current failure task=%s step=%s run=%s: %s', kwargs['task_id'],
                            kwargs['step_id'], kwargs['run_id'], exc)
                return Handling('ignored')
            raise FailureHandlingError(exc.code, str(exc)) from exc
        except Exception:
            logger.exception('Failure handling unavailable task=%s step=%s run=%s; using Planner decision',
                             kwargs['task_id'], kwargs['step_id'], kwargs['run_id'])
            return Handling('planner')
        answer = Handling(**handled)
        if answer.action in ('hold', 'breaker', 'planner'):
            self._history_tasks.add(kwargs['task_id'])
        if answer.hold:
            try:
                await self._prepare_quota_hold(answer.hold)
            except Exception:
                logger.exception('Could not persist scheduled quota deadline failure_id=%s', answer.failure['id'])
                await self._route_recovery_error(answer.hold)
                return answer
            self._schedule(answer.hold)
            if answer.hold['kind'] == 'quota' and answer.hold['provider_id']:
                from . import admission
                try:
                    if admission.enabled() and (await admission._controller.snapshot(answer.hold['provider_id']))['state'] != 'frozen':
                        self._schedule({**answer.hold, 'until': self.clock()})
                except Exception:
                    logger.exception('Could not recheck account after committed quota hold failure_id=%s', answer.failure['id'])
            # Notification failure cannot turn a committed hold into an unhandled run.
            try:
                await self._emit(kwargs['task_id'], {'event': 'failure_hold_opened', 'step_id': kwargs['step_id'],
                    'failure_id': answer.failure['id'], 'kind': answer.hold['kind'],
                    'cause': answer.hold['cause'], 'streak': answer.failure['streak']})
            except Exception:
                logger.exception('Could not announce failure hold task=%s step=%s failure_id=%s',
                                 kwargs['task_id'], kwargs['step_id'], answer.failure['id'])
        return answer

    async def note_success(self, **kwargs):
        if self.mode is not FailureTyping.ACT:
            return False
        try:
            return await store.record_success(**kwargs, pool=self.pool)
        except store.StoreError as exc:
            if exc.code in ('conflict', 'not_found'):
                return False
            raise FailureHandlingError(exc.code, str(exc)) from exc
        except Exception:
            logger.exception('Could not persist failure reset task=%s step=%s run=%s',
                             kwargs['task_id'], kwargs['step_id'], kwargs['run_id'])
            return False

    async def continuous_failures(self, task_id, step_id):
        return await store.continuous_failures(task_id, step_id, pool=self.pool)

    async def holds_view(self, task_id):
        if self.mode is not FailureTyping.ACT and task_id not in self._history_tasks:
            return None
        return await store.list_holds(task_id, pool=self.pool)

    async def get_holds(self, task_id):
        return await store.list_holds(task_id, pool=self.pool)

    async def _release(self, task_id, step_id, failure_id, action, *, actor, quota_exhausted=False):
        try:
            answer = await store.release_hold(task_id, step_id, failure_id, action, actor=actor,
                                              quota_exhausted=quota_exhausted, pool=self.pool)
        except store.StoreError as exc:
            raise FailureHandlingError(exc.code, str(exc)) from exc
        self._cancel_timer(step_id)
        try:
            await self._emit(task_id, answer)
        except Exception as exc:
            # The durable release outbox is replayed on restore, rather than silently losing recovery.
            logger.exception('Could not route committed failure release task=%s step=%s failure_id=%s',
                             task_id, step_id, failure_id)
            raise FailureHandlingError('unavailable', 'Hold released; recovery is pending and will resume after restoration') from exc
        return answer

    async def release_hold(self, task_id, step_id, failure_id, action, *, operator=False):
        if not operator:
            raise FailureHandlingError('forbidden', 'Only an authenticated operator may release failure holds')
        async with self._mode_lock:
            if self.mode is not FailureTyping.ACT:
                raise FailureHandlingError('conflict', 'Failure handling is no longer active; refresh the task')
            return await self._release(task_id, step_id, failure_id, action, actor='operator')

    async def consume_breaker_hold(self, task_id, step_id, failure_id):
        holds = await store.list_holds(task_id, pool=self.pool)
        matching = next((item for item in holds if item['step_id'] == step_id and item['failure_id'] == failure_id
                         and item['kind'] == 'breaker'), None)
        if matching is not None:
            await store.clear_task_holds(task_id, step_ids=[step_id], reason='Planner decision', pool=self.pool)
            self._cancel_timer(step_id)
            return True
        return False

    def _cancel_timer(self, step_id):
        task = self._timers.pop(step_id, None)
        if task is not None and task is not asyncio.current_task():
            task.cancel()

    @staticmethod
    def _has_quota_target(hold):
        return hold['kind'] == 'quota' or any(item.get('failure_class') == 'quota' for item in hold.get('targets', []))

    async def _prepare_quota_hold(self, hold):
        if not self._has_quota_target(hold):
            return
        schedule_id = await (self.pool or get_pool()).fetchval('SELECT schedule_id FROM tasks WHERE id=$1', hold['task_id'])
        if schedule_id:
            from .turn_admission import scheduled_quota_deadline
            quota_ids = [item['failure_id'] for item in hold.get('targets', []) if item.get('failure_class') == 'quota']
            started = await (self.pool or get_pool()).fetchval(
                'SELECT min(created_at) FROM step_failures WHERE id=ANY($1::bigint[])', quota_ids) if quota_ids else None
            self._quota_deadlines[hold['task_id']] = await scheduled_quota_deadline(
                hold['task_id'], schedule_id, self, started_at=started or hold['created_at'], pool=self.pool)

    def _schedule(self, hold, *, recovery_error=False):
        self._cancel_timer(hold['step_id'])
        if self.mode is not FailureTyping.ACT or self._closed:
            return
        if hold['kind'] == 'breaker' and hold['task_id'] not in self._quota_deadlines:
            return
        if hold['kind'] == 'environment' and hold.get('reminded_at') is not None and not self._has_quota_target(hold):
            return
        task = asyncio.create_task(self._timer(dict(hold), recovery_error=recovery_error), name=f"failure-hold-{hold['failure_id']}")
        self._timers[hold['step_id']] = task

    async def _timer(self, hold, *, recovery_error=False):
        try:
            if recovery_error:
                await self._sleep(60)
            await self._prepare_quota_hold(hold)
            deadline = self._quota_deadlines.get(hold['task_id']) if self._has_quota_target(hold) else None
            if hold['kind'] == 'breaker' and deadline is None:
                return
            if not recovery_error:
                due = deadline if hold['kind'] == 'breaker' else hold['until'] or (deadline if hold.get('reminded_at') is not None else None) or (
                    hold['created_at'] + timedelta(hours=1))
                if deadline is not None:
                    due = min(due, deadline)
                await self._sleep(max(0, (due - self.clock()).total_seconds()))
            if self.mode is not FailureTyping.ACT or self._closed or hold['task_id'] in self._cancelled:
                return
            current = next((item for item in await store.list_holds(hold['task_id'], pool=self.pool)
                            if item['step_id'] == hold['step_id'] and item['failure_id'] == hold['failure_id']), None)
            if current is None:
                if recovery_error:
                    for pending in await store.pending_releases(hold['task_id'], pool=self.pool):
                        if pending['failure_id'] == hold['failure_id']:
                            await self._emit(hold['task_id'], pending['payload'])
                return
            hold = current
            if deadline is not None and self.clock() >= deadline:
                async with self._mode_lock:
                    if self.mode is FailureTyping.ACT and not self._closed:
                        await self._release(hold['task_id'], hold['step_id'], hold['failure_id'], 'planner',
                                            actor='quota deadline', quota_exhausted=True)
                return
            if recovery_error:
                async with self._mode_lock:
                    if self.mode is FailureTyping.ACT and not self._closed:
                        await self._release(hold['task_id'], hold['step_id'], hold['failure_id'], 'planner',
                                            actor='recovery error')
                return
            if hold['kind'] == 'breaker':
                self._schedule(hold)  # a wall-clock correction can only defer expiry, never permit a fourth run
                return
            if hold['kind'] == 'environment':
                row = await (self.pool or get_pool()).fetchrow(
                    'UPDATE step_holds SET reminded_at=$2 WHERE failure_id=$1 AND reminded_at IS NULL RETURNING step_id',
                    hold['failure_id'], self.clock())
                if row is not None:
                    await self._emit(hold['task_id'], {'event': 'failure_hold_reminder', 'step_id': hold['step_id'],
                        'failure_id': hold['failure_id'], 'kind': 'environment', 'cause': hold['cause']})
                if deadline is not None:
                    self._schedule({**hold, 'reminded_at': self.clock()})
                return
            if hold['kind'] == 'quota' and hold['provider_id']:
                from . import admission
                if admission.enabled():
                    snapshot = await admission._controller.snapshot(hold['provider_id'])
                    if snapshot['state'] == 'frozen':
                        until = datetime.fromisoformat(snapshot['until']) if snapshot.get('until') else self.clock() + timedelta(minutes=5)
                        if until > self.clock():
                            await (self.pool or get_pool()).execute(
                                'UPDATE step_holds SET until=$2 WHERE failure_id=$1', hold['failure_id'], until)
                            self._schedule({**hold, 'until': until})
                            return
            async with self._mode_lock:
                if self.mode is FailureTyping.ACT and not self._closed:
                    await self._release(hold['task_id'], hold['step_id'], hold['failure_id'], 'retry', actor='timer')
        except asyncio.CancelledError:
            raise
        except FailureHandlingError as exc:
            if exc.code not in ('conflict', 'not_found'):
                logger.error('Failure hold timer could not recover failure_id=%s: %s', hold['failure_id'], exc)
                await self._route_recovery_error(hold, defer=recovery_error)
        except Exception:
            logger.exception('Failure hold timer failed task=%s step=%s failure_id=%s', hold['task_id'],
                             hold['step_id'], hold['failure_id'])
            await self._route_recovery_error(hold, defer=recovery_error)
        finally:
            if self._timers.get(hold['step_id']) is asyncio.current_task():
                self._timers.pop(hold['step_id'], None)

    async def _route_recovery_error(self, hold, *, defer=False):
        if self.mode is not FailureTyping.ACT or self._closed or hold['task_id'] in self._cancelled:
            return
        if not defer:
            try:
                async with self._mode_lock:
                    await self._release(hold['task_id'], hold['step_id'], hold['failure_id'], 'planner', actor='recovery error')
                return
            except FailureHandlingError as exc:
                if exc.code in ('conflict', 'not_found'):
                    return
                logger.error('Failure recovery routing remains pending failure_id=%s: %s', hold['failure_id'], exc)
            except Exception:
                logger.exception('Failure recovery routing remains pending failure_id=%s', hold['failure_id'])
        # Only retry the durable routing operation, never the model, while storage is unavailable.
        self._schedule(hold, recovery_error=True)

    async def restore_task(self, task_id):
        self._cancelled.discard(task_id)
        holds = await store.list_holds(task_id, pool=self.pool)
        if holds:
            self._history_tasks.add(task_id)
        for hold in holds:
            await self._prepare_quota_hold(hold)
            self._schedule(hold)
        for pending in await store.pending_releases(task_id, pool=self.pool):
            status = await (self.pool or get_pool()).fetchval('SELECT status FROM tasks WHERE id=$1', task_id)
            from ..db.tasks import ACTIVE_STATUSES
            if status in ACTIVE_STATUSES:
                await self._emit(task_id, pending['payload'])
            else:
                await store.mark_delivered(pending['failure_id'], pool=self.pool)
        return holds

    async def prepare_restore(self):
        """Read only the durable mode-exit marker before Planners start restoring."""
        settings = await system_settings.get_settings(['engine.failure_pending_recovery'], pool=self.pool)
        self.recovering = settings.get('engine.failure_pending_recovery') == 'true'

    async def restore(self):
        settings = await system_settings.get_settings(['engine.failure_pending_recovery'], pool=self.pool)
        if self.mode is not FailureTyping.ACT:
            if settings.get('engine.failure_pending_recovery') == 'true':
                await self._drain()
            return
        tasks = {item['task_id'] for item in await store.list_holds(pool=self.pool)}
        tasks.update(item['task_id'] for item in await store.pending_releases(pool=self.pool))
        for task_id in tasks:
            await self.restore_task(task_id)

    async def _drain(self):
        failed = False
        emitted = set()
        for hold in await store.list_holds(pool=self.pool):
            self._history_tasks.add(hold['task_id'])
            try:
                await self._release(hold['task_id'], hold['step_id'], hold['failure_id'], 'planner', actor='mode change')
                emitted.add(hold['failure_id'])
            except FailureHandlingError as exc:
                if exc.code in ('conflict', 'not_found'):
                    await self.cancel_task(hold['task_id'], reason='non-live task during mode drain')
                else:
                    failed = True
        for pending in await store.pending_releases(pool=self.pool):
            if pending['failure_id'] in emitted:
                continue
            try:
                from ..db.tasks import ACTIVE_STATUSES
                status = await (self.pool or get_pool()).fetchval('SELECT status FROM tasks WHERE id=$1', pending['task_id'])
                if status in ACTIVE_STATUSES:
                    await self._emit(pending['task_id'], pending['payload'])
                else:
                    await store.mark_delivered(pending['failure_id'], pool=self.pool)
            except Exception:
                failed = True
                logger.exception('Could not route pending mode recovery failure_id=%s', pending['failure_id'])
        if failed:
            raise FailureHandlingError('unavailable', 'Mode saved; some committed recoveries remain pending')
        self.recovering = await store.sync_recovery_marker(pool=self.pool)

    async def ack_release(self, failure_id):
        """Only the Planner that applied the committed recovery acknowledges its outbox row."""
        self.recovering = await store.mark_delivered(failure_id, pool=self.pool)

    async def cancel_task(self, task_id, *, reason='task ended', step_ids=None):
        if step_ids is None:
            self._cancelled.add(task_id)
            self._wake_waits()
        for hold in await store.list_holds(task_id, pool=self.pool):
            if step_ids is None or hold['step_id'] in step_ids:
                self._cancel_timer(hold['step_id'])
        return await store.clear_task_holds(task_id, reason=reason, step_ids=step_ids, pool=self.pool)

    async def set_mode(self, mode):
        selected = FailureTyping.OFF if mode is None else FailureTyping(mode)
        async with self._mode_lock:
            previous = self.mode
            values = {SETTING_KEY: None if mode is None else selected.value}
            if previous is FailureTyping.ACT and selected is not FailureTyping.ACT:
                values['engine.failure_pending_recovery'] = 'true'
                self.recovering = True
            await system_settings.put_settings(values, pool=self.pool)
            self.mode = selected
            self._wake_waits()
            if selected is FailureTyping.ACT:
                await self.restore()
                return
            for task in list(self._timers.values()):
                task.cancel()
            self._timers.clear()
            if previous is FailureTyping.ACT:
                await self._drain()

    async def recheck(self):
        if self.mode is not FailureTyping.ACT:
            return
        for hold in await store.list_holds(pool=self.pool):
            if hold['kind'] == 'quota':
                from . import admission
                if not admission.enabled():
                    self._schedule(hold)  # account admission is independent of the finite failure wait
                elif hold['provider_id'] and (await admission._controller.snapshot(hold['provider_id']))['state'] != 'frozen':
                    self._schedule({**hold, 'until': self.clock()})

    async def quota_deadline(self, task_id, max_seconds, *, started_at=None):
        deadline = await store.quota_deadline(task_id, max_seconds, now=started_at or self.clock(), pool=self.pool)
        self._quota_deadlines[task_id] = deadline
        return deadline

    async def clear_quota_deadline(self, task_id):
        await (self.pool or get_pool()).execute('DELETE FROM task_quota_waits WHERE task_id=$1', task_id)
        self._quota_deadlines.pop(task_id, None)

    async def operator_reset(self, task_id, *, operator, actor='operator'):
        return await store.operator_reset(task_id, operator=operator, actor=actor, pool=self.pool)

    async def stats(self):
        return await store.stats(pool=self.pool)

    async def close(self):
        self._closed = True
        self._wake_waits()
        tasks = list(self._timers.values())
        self._timers.clear()
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
