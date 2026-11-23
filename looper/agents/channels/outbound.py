"""Default-off outbound delivery: rate control and durable, keyed messages."""
from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import math
import random
import time
from collections import Counter, deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import StrEnum
from typing import Any
from uuid import uuid4
from weakref import WeakValueDictionary

from .base import PostOutcome, PostReceipt
from . import outbound_metrics
from .outbound_metrics import EventKind
from .lark_api import LarkApiError, MAX_RETRY_HINT
from .lark_cli import LarkCliError, MESSAGE_GONE_CODES, TIMEOUT

logger = logging.getLogger(__name__)
BACKOFF = (1, 2, 4, 8, 16)
JITTER = 0.2
MAX_RETRIES = 5
BREAKER_FAILURES = 5
BREAKER_WINDOW = 30
BREAKER_OPEN = 60
SEND_WAIT_SECONDS = 30
KEYED_DEDUPE_WINDOW = 3600
MAX_BACKOFF = 60
DELIVERY_DEADLINE = 900
OFF_RECOVERY_TIMEOUT = 30
STATE_WRITE_TIMEOUT = 5


class ErrorClass(StrEnum):
    RATE_LIMITED = 'rate_limited'
    REJECTED = 'rejected'
    TRANSIENT = 'transient'
    UNKNOWN = 'unknown'


class WriteKind(StrEnum):
    SEND = 'send'
    REPLY = 'reply'
    EDIT = 'edit'
    REACT = 'react'


class OutboxSource(StrEnum):
    ANSWER = 'answer'
    NOTICE = 'notice'
    PROGRESS = 'progress'
    POST = 'post'
    NOTIFY = 'notify'


class OutboundProtocol(StrEnum):
    LARK = 'lark'
    TELEGRAM = 'telegram'


def classify(exc: Exception) -> tuple[ErrorClass, float | None]:
    from .telegram_request import TelegramWriteError

    if isinstance(exc, TelegramWriteError):
        return exc.error_class, exc.retry_after
    retry_after = getattr(exc, 'retry_after', None)
    if isinstance(exc, LarkApiError) and exc.http_status == 429:
        return ErrorClass.RATE_LIMITED, retry_after
    if isinstance(exc, LarkCliError):
        if exc.code in {230020, 99991400}:
            return ErrorClass.RATE_LIMITED, retry_after
        # refused includes not_sent in the legacy transport; a call that never
        # started is safe to retry rather than a permanent platform rejection.
        if exc.refused and not exc.not_sent:
            return ErrorClass.REJECTED, None
        if exc.not_sent:
            return ErrorClass.TRANSIENT, None
        if exc.error_type == TIMEOUT:
            return ErrorClass.UNKNOWN, None
    return ErrorClass.UNKNOWN, None


def derived_key(channel_id: str, envelope_message_id: str, part: int) -> str:
    return hashlib.sha256(f'{channel_id}|{envelope_message_id}|{part}'.encode()).hexdigest()[:32]


def backoff(attempt: int, retry_after: float | None = None) -> float:
    delay = BACKOFF[min(max(attempt - 1, 0), len(BACKOFF) - 1)] * random.uniform(1 - JITTER, 1 + JITTER)
    if (isinstance(retry_after, bool) or not isinstance(retry_after, (int, float))
            or not 0 <= retry_after <= MAX_RETRY_HINT or not math.isfinite(retry_after)):
        retry_after = 0
    return min(MAX_BACKOFF, max(delay, retry_after))


class TokenBucket:
    def __init__(self, rate: float, capacity: float, clock=time.monotonic, *, sleep=asyncio.sleep):
        if rate <= 0 or capacity < 1:
            raise ValueError('rate must be positive and capacity must be at least one')
        self.rate, self.capacity, self.clock, self.sleep = rate, capacity, clock, sleep
        self.tokens, self.updated = capacity, clock()
        self._lock = asyncio.Lock()

    async def acquire(self):
        async with self._lock:
            while True:
                now = self.clock()
                self.tokens = min(self.capacity, self.tokens + (now - self.updated) * self.rate)
                self.updated = now
                if self.tokens >= 1 - 1e-9:
                    self.tokens = max(0, self.tokens - 1)
                    return
                await self.sleep((1 - self.tokens) / self.rate)


class BreakerState(StrEnum):
    CLOSED = 'closed'
    OPEN = 'open'
    HALF_OPEN = 'half_open'


class Breaker:
    def __init__(self, clock=time.monotonic, *, on_transition: Callable[[BreakerState], None] | None = None):
        self.clock = clock
        self._state = BreakerState.CLOSED
        self._on_transition = on_transition
        self.open_until = 0.0
        self._failures = deque()

    @property
    def state(self) -> BreakerState:
        return self._state

    @state.setter
    def state(self, state: BreakerState | str):
        state = BreakerState(state)
        previous, self._state = self._state, state
        if state != previous and self._on_transition is not None:
            self._on_transition(state)

    def allow(self) -> bool:
        if self.state == 'closed':
            return True
        if self.state == 'open' and self.clock() >= self.open_until:
            self.state = 'half_open'
            return True
        return False

    def record(self, result: bool | ErrorClass):
        if result is True:
            if self.state != 'closed':
                logger.info('outbound circuit recovered')
            self.state = 'closed'
            self._failures.clear()
            return
        if result == ErrorClass.REJECTED:
            # A rejection proves the platform is responding.
            if self.state == 'half_open':
                self.state = 'closed'
            self._failures.clear()
            return
        now = self.clock()
        self._failures.append(now)
        while self._failures and self._failures[0] < now - BREAKER_WINDOW:
            self._failures.popleft()
        if self.state == 'half_open' or len(self._failures) >= BREAKER_FAILURES:
            self.state = 'open'
            self.open_until = now + BREAKER_OPEN
            logger.warning('outbound circuit opened for %s seconds', BREAKER_OPEN)


class CircuitOpen(LarkCliError):
    pass


class DeliveryExpired(Exception):
    pass


class StateWriteUnavailable(Exception):
    pass


@dataclass
class Counters:
    backoffs: int = 0
    breaker_opens: int = 0
    db_fallbacks: int = 0
    failures: Counter = field(default_factory=Counter)


@dataclass
class _Edit:
    call: Callable[[], Awaitable[Any]]
    task: asyncio.Task | None = None
    last_at: float = float('-inf')


class _AppLimits:
    def __init__(self, settings, clock, sleep):
        self.loop = asyncio.get_running_loop()
        self.settings, self.clock, self.sleep = settings, clock, sleep
        self._limits = {}
        self._in_flight = 0
        self._slots = asyncio.Condition()
        self._configure()

    def _configure(self):
        limits = self.settings()
        for name, attr in (('send_qps', 'send_bucket'), ('write_qps', 'bucket')):
            rate = limits[name]
            if self._limits.get(name) != rate:
                setattr(self, attr, TokenBucket(rate, max(1, rate), self.clock, sleep=self.sleep))
        self._limits = dict(limits)

    @contextlib.asynccontextmanager
    async def _slot(self):
        async with self._slots:
            await self._slots.wait_for(lambda: self._in_flight < self.settings()['max_in_flight'])
            self._in_flight += 1
        try:
            yield
        finally:
            async with self._slots:
                self._in_flight -= 1
                self._slots.notify_all()


# Share allowances only within their owning loop; release them with the last gateway.
_APP_LIMITS: WeakValueDictionary[tuple[asyncio.AbstractEventLoop, str], _AppLimits] = WeakValueDictionary()


class Gateway:
    def __init__(self, channel_id: str, clock=time.monotonic, sleep=asyncio.sleep, *, settings=None, app_id: str = ''):
        self.channel_id, self.clock, self.sleep = channel_id, clock, sleep
        if settings is None:
            from ..chat_settings import outbound_limits
            settings = outbound_limits
        self.settings = settings
        # Unknown identities remain private; real adapters pass their verified app_id.
        self._app_id = app_id
        self._app_limits: _AppLimits | None = None
        self.counters = Counters()
        self.breaker = Breaker(clock, on_transition=self._breaker_transition)
        self._edits: dict[str, _Edit] = {}

    @property
    def _app(self) -> _AppLimits:
        # Adapters can be constructed before a loop starts or retained after it closes.
        loop = asyncio.get_running_loop()
        if self._app_limits is None or self._app_limits.loop is not loop:
            key = (loop, self._app_id)
            app = _APP_LIMITS.get(key) if self._app_id else None
            if app is None:
                app = _AppLimits(self.settings, self.clock, self.sleep)
                if self._app_id:
                    _APP_LIMITS[key] = app
            self._app_limits = app
        return self._app_limits

    def _breaker_transition(self, state: BreakerState):
        kind = {"open": EventKind.BREAKER_OPEN, "half_open": EventKind.BREAKER_HALF_OPEN,
                "closed": EventKind.BREAKER_RECOVERED}[state]
        outbound_metrics.events.record(self.channel_id, kind)
        if state == 'open':
            self.counters.breaker_opens += 1

    def record_backoff(self):
        self.counters.backoffs += 1
        outbound_metrics.events.record(self.channel_id, EventKind.BACKOFF)

    @property
    def bucket(self):
        return self._app.bucket

    @property
    def send_bucket(self):
        return self._app.send_bucket

    async def write(self, kind: WriteKind | str, call, *, keyed: bool = False, retry: bool = True):
        kind = WriteKind(kind)  # reject unsupported operations at the boundary
        started = self.clock()
        for attempt in range(MAX_RETRIES + 1):
            remaining = DELIVERY_DEADLINE - (self.clock() - started)
            if remaining <= 0:
                raise DeliveryExpired('delivery deadline expired')
            try:
                async with asyncio.timeout(remaining), self._app._slot():
                    self._app._configure()
                    if kind in {WriteKind.SEND, WriteKind.REPLY}:
                        await self.send_bucket.acquire()
                    await self.bucket.acquire()
                    if not self.breaker.allow():
                        raise CircuitOpen('outbound circuit is open')
                    result = await call()
            except (CircuitOpen, DeliveryExpired, StateWriteUnavailable):
                raise
            except Exception as exc:
                error_class, retry_after = classify(exc)
                self.counters.failures[error_class] += 1
                self.breaker.record(error_class)
                retryable = error_class in {ErrorClass.RATE_LIMITED, ErrorClass.TRANSIENT} or (
                    error_class == ErrorClass.UNKNOWN and keyed)
                if not retry or not retryable or attempt == MAX_RETRIES:
                    raise
                self.record_backoff()
                delay = backoff(attempt + 1, retry_after)
                if self.breaker.state == 'open':
                    delay = max(delay, self.breaker.open_until - self.clock())
                if self.clock() - started + delay >= DELIVERY_DEADLINE:
                    raise DeliveryExpired('delivery deadline expired') from exc
                await self.sleep(delay)
            else:
                self.breaker.record(True)
                return result

    async def stop(self):
        tasks = [item.task for item in self._edits.values() if item.task is not None and not item.task.done()]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._edits.clear()

    async def edit(self, message_id: str, call_with_text):
        if self.breaker.state == 'half_open' or (self.breaker.state == 'open' and
                                                  self.clock() < self.breaker.open_until):
            raise CircuitOpen('outbound circuit is open')
        item = self._edits.get(message_id)
        if item is None:
            item = self._edits[message_id] = _Edit(call_with_text)
        item.call = call_with_text
        if item.task is None or item.task.done():
            async def flush():
                delay = self.settings()['edit_interval_seconds'] - (self.clock() - item.last_at)
                if delay > 0:
                    await self.sleep(delay)
                while True:
                    call = item.call
                    async def transmit():
                        # Rate/slot waits must not shorten the interval between actual edits.
                        remaining = self.settings()['edit_interval_seconds'] - (self.clock() - item.last_at)
                        if remaining > 0:
                            await self.sleep(remaining)
                        item.last_at = self.clock()
                        return await call()
                    result = await self.write(WriteKind.EDIT, transmit)
                    if item.call is call:
                        return result
                    await self.sleep(max(0, self.settings()['edit_interval_seconds'] - (self.clock() - item.last_at)))
            item.task = asyncio.create_task(flush())
        return await asyncio.shield(item.task)


class Outbox:
    def __init__(self, channel_id, transport, gateway, db=None, clock=None, *,
                 protocol: OutboundProtocol = OutboundProtocol.LARK, enabled: Callable[[], bool] = lambda: True):
        if db is None:
            from ...db import channel_outbox
            db = channel_outbox
        self.channel_id, self.transport, self.gateway, self.db = channel_id, transport, gateway, db
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.protocol = OutboundProtocol(protocol)
        self.enabled = enabled
        self._start_lock = asyncio.Lock()
        self._wake = asyncio.Event()
        self._task: asyncio.Task | None = None
        self._recovery_task: asyncio.Task | None = None
        self._workers: set[asyncio.Task] = set()
        self._waiters: dict[int, asyncio.Future] = {}

    async def start_if_needed(self):
        # Off only gates new inserts; persisted work keeps its delivery promise.
        if self.enabled():
            await self.start()
            return
        if self._task is None and (self._recovery_task is None or self._recovery_task.done()):
            self._recovery_task = asyncio.create_task(self._recover_if_needed())

    async def _recover_if_needed(self):
        try:
            async with asyncio.timeout(OFF_RECOVERY_TIMEOUT):
                if await self.db.has_unfinished(self.channel_id):
                    await self.start()
        except Exception:
            # OFF preserves legacy startup and logging even with an exhausted pool.
            # Persisted work is probed again at the next startup.
            return

    async def start(self):
        async with self._start_lock:
            if self._task is not None:
                return
            await self.db.recover(self.channel_id, self.clock(),
                                  KEYED_DEDUPE_WINDOW if self.protocol == OutboundProtocol.LARK else 0)
            self._task = asyncio.create_task(self._run())

    async def stop(self):
        # Join the probe first: it may be about to start the scanner or new workers.
        if self._recovery_task is not None:
            self._recovery_task.cancel()
            await asyncio.gather(self._recovery_task, return_exceptions=True)
            self._recovery_task = None
        tasks = [*self._workers, *([self._task] if self._task else [])]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._task = None
        self._workers.clear()
        for future in self._waiters.values():
            future.cancel()
        self._waiters.clear()

    async def _call(self, row):
        body = dict(row['body'])
        body.update(idempotency_key=row['idempotency_key'], return_id=True)
        if row['kind'] == 'reply':
            return await self.transport.reply(row['reply_to'], **body)
        if not ('chat_id' in body or 'user_id' in body):
            body['chat_id'] = row['chat']
        return await self.transport.send(**body)

    @staticmethod
    def _receipt(row):
        if row['state'] == 'sent':
            return PostReceipt(True, external_ids=(row['external_id'],) if row['external_id'] else (),
                               outcome=PostOutcome.SENT)
        raw_error = row.get('error')
        try:
            detail = json.loads(raw_error) if raw_error else {}
        except (ValueError, TypeError):
            detail = {}
        if not isinstance(detail, dict):
            detail = {}
        return PostReceipt(False, error=detail.get('message', raw_error),
                           parent_gone=detail.get('code') in MESSAGE_GONE_CODES,
                           outcome=PostOutcome.REJECTED if row.get('error_class') == ErrorClass.REJECTED
                           else PostOutcome.UNKNOWN)

    async def send(self, kind, chat, body, reply_to, key, source, *, replace_rejected: bool = False):
        if WriteKind(kind) not in {WriteKind.SEND, WriteKind.REPLY}:
            raise ValueError('outbox only accepts sends and replies')
        OutboxSource(source)
        if replace_rejected and self.protocol != OutboundProtocol.TELEGRAM:
            raise ValueError('rejected body replacement is only supported for Telegram')
        if not isinstance(key, str) or not 1 <= len(key) <= 50:
            raise ValueError('idempotency key must contain 1 to 50 characters')
        row = dict(channel_id=self.channel_id, idempotency_key=key, kind=kind,
                   chat=chat, body=body, reply_to=reply_to, source=source)
        try:
            row = await self.db.insert_or_get(row)
        except Exception:
            if replace_rejected:
                # A fallback requires proof that this row's preceding payload was refused.
                return PostReceipt(False, outcome=PostOutcome.UNKNOWN, error='cannot verify rejected delivery')
            self.gateway.counters.db_fallbacks += 1
            logger.warning('outbound outbox insert failed; sending directly through gateway', exc_info=True)
            try:
                external_id = await self.gateway.write(kind, lambda: self._call(row),
                                                       keyed=self.protocol == OutboundProtocol.LARK)
                return PostReceipt(True, external_ids=(external_id,) if external_id else (), outcome=PostOutcome.SENT)
            except Exception as exc:
                error_class, _ = classify(exc)
                return PostReceipt(False, error=str(exc), parent_gone=getattr(exc, 'code', None) in MESSAGE_GONE_CODES,
                                   outcome=PostOutcome.REJECTED if error_class == ErrorClass.REJECTED
                                   else PostOutcome.UNKNOWN)
        if replace_rejected:
            try:
                row = await self.db.replace_rejected_body(row['id'], body)
            except Exception:
                logger.warning('outbound rejected body replacement failed; delivery remains unknown', exc_info=True)
                return PostReceipt(False, outcome=PostOutcome.UNKNOWN, error='cannot verify rejected delivery')
        if row['state'] in {'sent', 'failed', 'uncertain'}:
            return self._receipt(row)
        future = self._waiters.setdefault(row['id'], asyncio.get_running_loop().create_future())
        self._wake.set()
        if self._task is None:
            await self.start()
        # One read closes insert/register and startup-recovery races.
        current = await self.db.get(row['id'])
        if current['state'] in {'sent', 'failed', 'uncertain'} and not future.done():
            future.set_result(self._receipt(current))
            self._waiters.pop(row['id'], None)
        try:
            return await asyncio.wait_for(asyncio.shield(future), SEND_WAIT_SECONDS)
        except asyncio.TimeoutError:
            return PostReceipt(False, outcome=PostOutcome.UNKNOWN, error='outbound delivery is still pending')

    async def _run(self):
        while True:
            self._wake.clear()
            delay = None
            try:
                while True:
                    row = await self.db.next_due(self.channel_id, self.clock(),
                                                 expired_before=self.clock() - timedelta(seconds=DELIVERY_DEADLINE))
                    if row is None:
                        break
                    if reason := self._expiry(row):
                        await self._expire(row, reason)
                        await self._complete(row)
                        continue
                    breaker = self.gateway.breaker
                    if breaker.state == 'half_open':
                        break
                    remaining = breaker.open_until - self.gateway.clock()
                    if breaker.state == 'open' and remaining > 0:
                        await self.db.defer_unsent(row['id'], self.clock() + timedelta(seconds=remaining))
                        continue
                    claim_token = uuid4()
                    if not await self._persist(lambda: self.db.mark_sending(row['id'], claim_token=claim_token)):
                        continue
                    row['attempts'] += 1
                    task = asyncio.create_task(self._deliver(row))
                    self._workers.add(task)
                    task.add_done_callback(self._finished)
                at = await self.db.next_attempt(self.channel_id, delivery_deadline=DELIVERY_DEADLINE)
                if at is not None and self.gateway.breaker.state != 'half_open':
                    delay = max(.01, (at - self.clock()).total_seconds())
            except Exception:
                logger.exception('outbound outbox scan failed')
                delay = 1
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self._wake.wait(), delay)

    def _finished(self, task):
        self._workers.discard(task)
        if not task.cancelled() and task.exception():
            logger.error('outbound delivery state write failed', exc_info=task.exception())
        self._wake.set()

    async def _persist(self, call, *, timeout=None):
        try:
            # The optional total deadline includes calls and retry sleeps. A known
            # platform result keeps retrying outside the app slot until it is saved.
            async with asyncio.timeout(timeout):
                attempt = 0
                while True:
                    try:
                        async with asyncio.timeout(STATE_WRITE_TIMEOUT):
                            return await call()
                    except Exception:
                        attempt += 1
                        logger.warning('outbound state write unavailable; retrying without resending', exc_info=True)
                        await asyncio.sleep(min(MAX_BACKOFF, 2 ** min(attempt - 1, 6)))
        except TimeoutError as exc:
            raise StateWriteUnavailable('outbound state write deadline expired') from exc

    def _expiry(self, row, *, attempting: bool = False):
        now = self.clock()
        first = row.get('first_possible_at')
        if first is not None and ((self.protocol == OutboundProtocol.TELEGRAM and not attempting) or
                                  (self.protocol == OutboundProtocol.LARK and
                                   (now - first).total_seconds() >= KEYED_DEDUPE_WINDOW)):
            return 'platform deduplication window expired'
        created = row.get('created_at', now)
        if (now - created).total_seconds() >= DELIVERY_DEADLINE:
            return 'delivery deadline expired'
        return None

    async def _expire(self, row, reason):
        if row.get('first_possible_at') is not None:
            await self._persist(lambda: self.db.mark_uncertain(row['id'], reason))
        else:
            await self._persist(lambda: self.db.mark_failed(row['id'], ErrorClass.TRANSIENT, reason))

    async def _attempt(self, row):
        # This runs inside the shared slot, after token waits, immediately before I/O.
        if reason := self._expiry(row):
            raise DeliveryExpired(reason)
        attempted_at = self.clock()
        row['first_possible_at'] = await self._persist(lambda: self.db.mark_attempted(row['id'], attempted_at),
                                                      timeout=STATE_WRITE_TIMEOUT)
        if row['first_possible_at'] is None:
            raise DeliveryExpired('delivery claim no longer active')
        if reason := self._expiry(row, attempting=True):
            raise DeliveryExpired(reason)
        remaining = DELIVERY_DEADLINE - (self.clock() - row.get('created_at', self.clock())).total_seconds()
        async with asyncio.timeout(max(0, remaining)):
            return await self._call(row)

    async def _deliver(self, row):
        breaker = self.gateway.breaker
        if breaker.state != 'closed' and breaker.open_until > self.gateway.clock():
            await self._persist(lambda: self.db.defer_unsent(row['id'], self.clock() + timedelta(
                seconds=breaker.open_until - self.gateway.clock())))
            return
        try:
            external_id = await self.gateway.write(row['kind'], lambda: self._attempt(row), keyed=True, retry=False)
        except StateWriteUnavailable:
            # No platform call happened. Defer outside the shared app slot even if
            # the database also remains unavailable for this state write.
            await self._persist(lambda: self.db.defer_unsent(row['id'],
                                self.clock() + timedelta(seconds=backoff(1))))
        except DeliveryExpired as exc:
            await self._expire(row, str(exc))
        except CircuitOpen:
            await self._persist(lambda: self.db.defer_unsent(row['id'], self.clock() + timedelta(
                seconds=max(0.1, breaker.open_until - self.gateway.clock()))))
        except Exception as exc:
            error_class, retry_after = classify(exc)
            error = json.dumps({'code': getattr(exc, 'code', None), 'message': str(exc)[:200]},
                               ensure_ascii=False)
            if error_class == ErrorClass.UNKNOWN and self.protocol != OutboundProtocol.LARK:
                await self._persist(lambda: self.db.mark_uncertain(row['id'], error))
            elif error_class == ErrorClass.REJECTED or row['attempts'] > MAX_RETRIES:
                await self._persist(lambda: self.db.mark_failed(row['id'], error_class, error))
            else:
                self.gateway.record_backoff()
                delay = backoff(row['attempts'], retry_after)
                if breaker.state == 'open':
                    delay = max(delay, breaker.open_until - self.gateway.clock())
                at = min(self.clock() + timedelta(seconds=delay),
                         row.get('created_at', self.clock()) + timedelta(seconds=DELIVERY_DEADLINE))
                await self._persist(lambda: self.db.retry_later(row['id'], at, error_class, error,
                    **({'clear_possible': True} if self.protocol == OutboundProtocol.TELEGRAM else {})))
        else:
            await self._persist(lambda: self.db.mark_sent(row['id'], external_id))
        await self._complete(row)

    async def _complete(self, row):
        current = await self._persist(lambda: self.db.get(row['id']))
        if current['state'] in {'sent', 'failed', 'uncertain'}:
            future = self._waiters.pop(row['id'], None)
            if future is not None and not future.done():
                future.set_result(self._receipt(current))
