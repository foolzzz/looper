"""Default-off outbound delivery: rate control and durable, keyed messages."""
from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import random
import time
from collections import Counter, deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import StrEnum
from typing import Any

from .base import PostOutcome, PostReceipt
from .lark_api import LarkApiError
from .lark_cli import LarkCliError, MESSAGE_GONE_CODES, TIMEOUT

logger = logging.getLogger(__name__)
RATE_PER_SECOND = 15
BACKOFF = (1, 2, 4, 8, 16)
JITTER = 0.2
MAX_RETRIES = 5
BREAKER_FAILURES = 5
BREAKER_WINDOW = 30
BREAKER_OPEN = 60
EDIT_FLOOR = 1.0
SEND_WAIT_SECONDS = 30
KEYED_DEDUPE_WINDOW = 3600


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
    return max(delay, retry_after or 0)


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


class Breaker:
    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self.state = 'closed'
        self.open_until = 0.0
        self._failures = deque()

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


class Gateway:
    def __init__(self, channel_id: str, clock=time.monotonic, sleep=asyncio.sleep):
        self.channel_id, self.clock, self.sleep = channel_id, clock, sleep
        self.bucket = TokenBucket(RATE_PER_SECOND, RATE_PER_SECOND, clock, sleep=sleep)
        self.breaker = Breaker(clock)
        self.counters = Counters()
        self._edits: dict[str, _Edit] = {}

    async def write(self, kind: WriteKind | str, call, *, keyed: bool = False, retry: bool = True):
        WriteKind(kind)  # reject unsupported operations at the boundary
        for attempt in range(MAX_RETRIES + 1):
            await self.bucket.acquire()
            if not self.breaker.allow():
                raise CircuitOpen('outbound circuit is open')
            try:
                result = await call()
            except Exception as exc:
                error_class, retry_after = classify(exc)
                self.counters.failures[error_class] += 1
                previous = self.breaker.state
                self.breaker.record(error_class)
                if self.breaker.state == 'open' and previous != 'open':
                    self.counters.breaker_opens += 1
                retryable = error_class in {ErrorClass.RATE_LIMITED, ErrorClass.TRANSIENT} or (
                    error_class == ErrorClass.UNKNOWN and keyed)
                if not retry or not retryable or attempt == MAX_RETRIES:
                    raise
                self.counters.backoffs += 1
                delay = backoff(attempt + 1, retry_after)
                if self.breaker.state == 'open':
                    delay = max(delay, self.breaker.open_until - self.clock())
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
                delay = EDIT_FLOOR - (self.clock() - item.last_at)
                if delay > 0:
                    await self.sleep(delay)
                while True:
                    call = item.call
                    item.last_at = self.clock()
                    result = await self.write(WriteKind.EDIT, call)
                    if item.call is call:
                        return result
                    await self.sleep(max(0, EDIT_FLOOR - (self.clock() - item.last_at)))
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
        self._workers: set[asyncio.Task] = set()

    async def start(self):
        async with self._start_lock:
            if self._task is not None:
                return
            await self.db.recover(self.channel_id, self.clock(),
                                  KEYED_DEDUPE_WINDOW if self.protocol == OutboundProtocol.LARK else 0)
            self._task = asyncio.create_task(self._run())

    async def stop(self):
        tasks = [*self._workers, *([self._task] if self._task else [])]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._task = None
        self._workers.clear()

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

    async def send(self, kind, chat, body, reply_to, key, source):
        if WriteKind(kind) not in {WriteKind.SEND, WriteKind.REPLY}:
            raise ValueError('outbox only accepts sends and replies')
        OutboxSource(source)
        if not isinstance(key, str) or not 1 <= len(key) <= 50:
            raise ValueError('idempotency key must contain 1 to 50 characters')
        row = dict(channel_id=self.channel_id, idempotency_key=key, kind=kind,
                   chat=chat, body=body, reply_to=reply_to, source=source)
        try:
            row = await self.db.insert_or_get(row)
        except Exception:
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
        self._wake.set()
        if self._task is None:
            await self.start()
        async def wait():
            nonlocal row
            while row['state'] not in {'sent', 'failed', 'uncertain'}:
                await asyncio.sleep(0.05)
                row = await self.db.get(row['id'])
            return self._receipt(row)
        try:
            return await asyncio.wait_for(wait(), SEND_WAIT_SECONDS)
        except asyncio.TimeoutError:
            return PostReceipt(False, outcome=PostOutcome.UNKNOWN, error='outbound delivery is still pending')

    async def _run(self):
        while True:
            self._wake.clear()
            try:
                # Already claimed calls may finish; disabling pauses new work.
                while self.enabled():
                    row = await self.db.next_due(self.channel_id, self.clock())
                    if row is None or not self.enabled():
                        break
                    breaker = self.gateway.breaker
                    if breaker.state == 'half_open':
                        break
                    remaining = breaker.open_until - self.gateway.clock()
                    if breaker.state == 'open' and remaining > 0:
                        await self.db.defer_unsent(row['id'], self.clock() + timedelta(seconds=remaining))
                        continue
                    if not await self.db.mark_sending(row['id']):
                        continue
                    row['attempts'] += 1
                    task = asyncio.create_task(self._deliver(row))
                    self._workers.add(task)
                    task.add_done_callback(self._finished)
            except Exception:
                logger.exception('outbound outbox scan failed')
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self._wake.wait(), 0.1)

    def _finished(self, task):
        self._workers.discard(task)
        if not task.cancelled() and task.exception():
            logger.error('outbound delivery state write failed', exc_info=task.exception())
        self._wake.set()

    async def _deliver(self, row):
        breaker = self.gateway.breaker
        if breaker.state != 'closed' and breaker.open_until > self.gateway.clock():
            await self.db.defer_unsent(row['id'], self.clock() + timedelta(
                seconds=breaker.open_until - self.gateway.clock()))
            return
        try:
            external_id = await self.gateway.write(row['kind'], lambda: self._call(row), keyed=True, retry=False)
        except CircuitOpen:
            await self.db.defer_unsent(row['id'], self.clock() + timedelta(
                seconds=max(0.1, breaker.open_until - self.gateway.clock())))
        except Exception as exc:
            error_class, retry_after = classify(exc)
            error = json.dumps({'code': getattr(exc, 'code', None), 'message': str(exc)[:200]},
                               ensure_ascii=False)
            if error_class == ErrorClass.UNKNOWN and self.protocol != OutboundProtocol.LARK:
                await self.db.mark_uncertain(row['id'], error)
            elif error_class == ErrorClass.REJECTED or row['attempts'] > MAX_RETRIES:
                await self.db.mark_failed(row['id'], error_class, error)
            else:
                self.gateway.counters.backoffs += 1
                delay = backoff(row['attempts'], retry_after)
                if breaker.state == 'open':
                    delay = max(delay, breaker.open_until - self.gateway.clock())
                await self.db.retry_later(row['id'], self.clock() + timedelta(seconds=delay), error_class, error)
        else:
            await self.db.mark_sent(row['id'], external_id)
