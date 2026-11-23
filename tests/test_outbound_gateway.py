from __future__ import annotations

import asyncio

import pytest

from looper.agents.channels.lark_api import LarkApiError
from looper.agents.channels.lark_cli import LarkCliError, TIMEOUT
from looper.agents.channels.outbound import (
    BACKOFF, Breaker, CircuitOpen, ErrorClass, Gateway, TokenBucket, classify, derived_key,
)


class Clock:
    def __init__(self):
        self.now = 0.0
        self.waits = []

    def __call__(self):
        return self.now

    async def sleep(self, seconds):
        self.waits.append(seconds)
        self.now += seconds
        await asyncio.sleep(0)


@pytest.mark.parametrize(('exc', 'expected'), [
    (LarkCliError('rate', code=230020), ErrorClass.RATE_LIMITED),
    (LarkCliError('rate', code=99991400), ErrorClass.RATE_LIMITED),
    (LarkApiError('rate', http_status=429), ErrorClass.RATE_LIMITED),
    (LarkApiError('rate', http_status=429, code=999), ErrorClass.RATE_LIMITED),
    (LarkCliError('validation', error_type='validation'), ErrorClass.REJECTED),
    (LarkCliError('start', not_sent=True), ErrorClass.TRANSIENT),
    (LarkCliError('timeout', error_type=TIMEOUT), ErrorClass.UNKNOWN),
    (RuntimeError('lost connection'), ErrorClass.UNKNOWN),
])
def test_classify(exc, expected):
    assert classify(exc) == (expected, None)


def test_bucket_thirty_requests_take_one_second():
    async def run():
        clock = Clock()
        bucket = TokenBucket(15, 15, clock, sleep=clock.sleep)
        for _ in range(30):
            await bucket.acquire()
        assert clock.now == pytest.approx(1)
    asyncio.run(run())


def test_breaker_single_probe_and_recovery():
    clock = Clock()
    breaker = Breaker(clock)
    for _ in range(5):
        breaker.record(ErrorClass.UNKNOWN)
    assert breaker.state == 'open'
    assert not breaker.allow()
    clock.now = 60
    assert breaker.allow()
    assert breaker.state == 'half_open'
    assert not breaker.allow()
    breaker.record(ErrorClass.TRANSIENT)
    assert breaker.state == 'open'
    clock.now = 120
    assert breaker.allow()
    breaker.record(True)
    assert breaker.state == 'closed'


def test_backoff_retry_after_and_unknown_key():
    async def run():
        clock = Clock()
        gateway = Gateway('test', clock, clock.sleep)
        calls = 0
        async def call():
            nonlocal calls
            calls += 1
            if calls <= 2:
                error = LarkApiError('rate', http_status=429)
                error.retry_after = 3
                raise error
            return 'om_sent'
        assert await gateway.write('send', call, keyed=True) == 'om_sent'
        assert calls == 3
        assert clock.waits == [3, 3]
        assert gateway.counters.backoffs == 2
        async def unknown():
            raise LarkCliError('timeout', error_type=TIMEOUT)
        with pytest.raises(LarkCliError):
            await gateway.write('send', unknown)
        assert gateway.counters.failures[ErrorClass.UNKNOWN] == 1
    asyncio.run(run())


def test_rejected_is_not_retried_and_open_edits_fail():
    async def run():
        clock = Clock()
        gateway = Gateway('test', clock, clock.sleep)
        async def call():
            raise LarkCliError('bad', code=123)
        with pytest.raises(LarkCliError):
            await gateway.write('reply', call, keyed=True)
        assert clock.waits == []
        for _ in range(5):
            gateway.breaker.record(ErrorClass.UNKNOWN)
        with pytest.raises(CircuitOpen):
            await gateway.write('edit', call)
    asyncio.run(run())


def test_edits_coalesce_latest_text():
    async def run():
        gateway = Gateway('test')
        calls = []
        async def edit(text):
            calls.append(text)
        await gateway.edit('om_test', lambda: edit('one'))
        await asyncio.gather(gateway.edit('om_test', lambda: edit('two')),
                             gateway.edit('om_test', lambda: edit('three')))
        assert calls == ['one', 'three']
    asyncio.run(run())


def test_key_is_stable():
    assert derived_key('channel', 'message', 0) == 'af5c91cb8448f52b066a5f9a563c08e6'
    assert len(BACKOFF) == 5


def test_retry_schedule_jitter_and_exhaustion():
    from looper.agents.channels.outbound import backoff
    for attempt, base in enumerate(BACKOFF, 1):
        for _ in range(20):
            assert base * .8 <= backoff(attempt) <= base * 1.2
    async def run():
        clock = Clock()
        gateway = Gateway('test', clock, clock.sleep)
        calls = 0
        async def call():
            nonlocal calls
            calls += 1
            raise LarkCliError('busy', code=230020)
        with pytest.raises(LarkCliError):
            await gateway.write('send', call, keyed=True)
        assert calls == 6
        assert gateway.counters.backoffs == 5
        assert gateway.counters.breaker_opens == 2
    asyncio.run(run())


def test_keyed_timeouts_retry_but_unkeyed_do_not():
    async def run():
        clock = Clock()
        gateway = Gateway('test', clock, clock.sleep)
        calls = 0
        async def call():
            nonlocal calls
            calls += 1
            if calls <= 2:
                raise LarkCliError('timeout after delivery', error_type=TIMEOUT)
            return 'om_keyed'
        assert await gateway.write('reply', call, keyed=True) == 'om_keyed'
        assert calls == 3
        assert len(clock.waits) == 2
    asyncio.run(run())


def test_outbox_insert_failure_falls_back_to_gateway():
    from looper.agents.channels.outbound import Outbox
    async def run():
        class BrokenDatabase:
            async def insert_or_get(self, row):
                raise OSError('database unavailable')
        class Transport:
            async def send(self, **kwargs):
                assert kwargs == dict(chat_id='oc_test', text='hello', idempotency_key='key', return_id=True)
                return 'om_sent'
        gateway = Gateway('test')
        receipt = await Outbox('test', Transport(), gateway, BrokenDatabase()).send(
            'send', 'oc_test', {'text': 'hello'}, None, 'key', 'answer')
        assert receipt.success
        assert receipt.external_ids == ('om_sent',)
        assert gateway.counters.db_fallbacks == 1
    asyncio.run(run())


def test_breaker_ignores_rejections_and_expires_old_failures():
    clock = Clock()
    breaker = Breaker(clock)
    for _ in range(4):
        breaker.record(ErrorClass.TRANSIENT)
    clock.now = 31
    breaker.record(ErrorClass.UNKNOWN)
    assert breaker.state == 'closed'
    breaker.record(ErrorClass.REJECTED)
    for _ in range(4):
        breaker.record(ErrorClass.TRANSIENT)
    assert breaker.state == 'closed'


def test_outbox_receipt_uses_structured_platform_code():
    import json
    from looper.agents.channels.outbound import Outbox
    from looper.agents.channels.base import PostOutcome
    receipt = Outbox._receipt({'state': 'failed', 'error_class': 'rejected',
                              'error': json.dumps({'code': 230011, 'message': 'parent is gone'})})
    assert receipt.parent_gone
    assert receipt.result == PostOutcome.REJECTED
    assert receipt.error == 'parent is gone'
    receipt = Outbox._receipt({'state': 'uncertain', 'error_class': 'unknown', 'error': 'lost response'})
    assert receipt.result == PostOutcome.UNKNOWN
    assert not receipt.parent_gone


def test_gateway_stop_cancels_shielded_edits_before_transport_closes():
    import contextlib
    async def run():
        gateway = Gateway('test')
        started = asyncio.Event()
        cancelled = asyncio.Event()
        async def call():
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()
        waiter = asyncio.create_task(gateway.edit('om_test', call))
        await started.wait()
        waiter.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await waiter
        assert not cancelled.is_set()
        await gateway.stop()
        assert cancelled.is_set()
        assert gateway._edits == {}
    asyncio.run(run())


def test_twenty_chats_share_four_in_flight_slots():
    async def run():
        gateway = Gateway('app')
        active = peak = 0
        async def call():
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(.02)
            active -= 1
        await asyncio.gather(*(gateway.write(kind, call) for kind in ['send', 'reply', 'edit', 'react'] * 5))
        assert peak <= 4
        assert peak > 1
    asyncio.run(run())


def test_distinct_rate_buckets_and_live_limits():
    async def run():
        clock = Clock()
        limits = dict(send_qps=2, write_qps=3, edit_interval_seconds=2, max_in_flight=1)
        gateway = Gateway('app', clock, clock.sleep, settings=lambda: limits)
        async def call():
            return True
        for _ in range(4):
            await gateway.write('send', call)
        assert clock.now == pytest.approx(1)
        # Reactions consume the shared write bucket but no send tokens.
        for _ in range(3):
            await gateway.write('react', call)
        assert clock.now == pytest.approx(4 / 3)
        await gateway.edit('message', call)
        before = clock.now
        await gateway.edit('message', call)
        assert clock.now - before + 1e-9 >= 2
        limits['edit_interval_seconds'] = 3
        before = clock.now
        await gateway.edit('message', call)
        assert clock.now - before + 1e-9 >= 3
    asyncio.run(run())


@pytest.mark.parametrize('hint', [86400000, float('inf'), float('nan'), -1, 'invalid', True, 10**1000])
def test_abnormal_retry_hints_do_not_stall_delivery(hint):
    from looper.agents.channels.outbound import backoff
    # Invalid platform hints must fall back to the ordinary jittered delay.
    assert .8 <= backoff(1, hint) <= 1.2


def test_valid_retry_hints_are_clamped():
    from looper.agents.channels.outbound import backoff
    assert 1 <= backoff(1, 300) <= 60
