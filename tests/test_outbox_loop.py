"""Delivery-loop contracts with a deterministic in-memory persistence boundary."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest

from looper.agents.channels import outbound
from looper.agents.channels.base import PostOutcome


class Database:
    def __init__(self):
        self.rows = {}
        self.recovers = 0
        self.scans = 0

    async def recover(self, *args):
        self.recovers += 1
        await asyncio.sleep(0)

    async def insert_or_get(self, row):
        for previous in self.rows.values():
            if previous['idempotency_key'] == row['idempotency_key']:
                return dict(previous)
        row = dict(row, id=len(self.rows) + 1, state='pending', attempts=0,
                   next_attempt_at=datetime.now(timezone.utc), error=None)
        self.rows[row['id']] = row
        return dict(row)

    async def get(self, id):
        return dict(self.rows[id])

    async def next_due(self, channel, now, **kwargs):
        self.scans += 1
        for row in self.rows.values():
            if row['state'] == 'pending' and row['next_attempt_at'] <= now and not any(
                prior['chat'] == row['chat'] and prior['id'] < row['id'] and
                prior['state'] in {'pending', 'sending'} for prior in self.rows.values()
            ):
                return dict(row)
        return None

    async def next_attempt(self, channel, **kwargs):
        return min((row['next_attempt_at'] for row in self.rows.values()
                    if row['state'] == 'pending' and not any(
                        prior['chat'] == row['chat'] and prior['id'] < row['id'] and
                        prior['state'] in {'pending', 'sending'} for prior in self.rows.values())), default=None)

    async def mark_sending(self, id, *, claim_token=None):
        self.rows[id]['state'] = 'sending'
        self.rows[id]['attempts'] += 1
        return True

    async def mark_attempted(self, id, now):
        row = self.rows[id]
        if row.get('first_possible_at') is None:
            row['first_possible_at'] = now
        return row['first_possible_at']

    async def mark_sent(self, id, external):
        self.rows[id].update(state='sent', external_id=external)

    async def defer_unsent(self, id, at):
        row = self.rows[id]
        row.update(state='pending', next_attempt_at=at,
                   attempts=row['attempts'] - (row['state'] == 'sending'))

    async def retry_later(self, id, at, error_class, error):
        self.rows[id].update(state='pending', next_attempt_at=at, error_class=error_class, error=error)


async def until(predicate):
    async def wait():
        while not predicate():
            await asyncio.sleep(.005)
    await asyncio.wait_for(wait(), 2)


@pytest.mark.parametrize('finish', ['deadline', 'stop'])
def test_disabled_recovery_is_bounded_and_cancelled_at_shutdown(monkeypatch, finish):
    monkeypatch.setattr(outbound, 'OFF_RECOVERY_TIMEOUT', .03 if finish == 'deadline' else 30)

    async def run():
        entered, cancelled = asyncio.Event(), asyncio.Event()
        async def blocked_probe(*args):
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()
        db = AsyncMock()
        db.has_unfinished.side_effect = blocked_probe
        box = outbound.Outbox('app', AsyncMock(), outbound.Gateway('app'), db, enabled=lambda: False)
        try:
            await asyncio.wait_for(box.start_if_needed(), .01)
            await asyncio.wait_for(entered.wait(), 1)
            # Repeated adapter starts share a single active probe.
            await asyncio.wait_for(box.start_if_needed(), .01)
            if finish == 'stop':
                await box.stop()
            await asyncio.wait_for(cancelled.wait(), .2)
            db.has_unfinished.assert_awaited_once()
            db.recover.assert_not_awaited()
            assert box._task is None
        finally:
            await box.stop()
    asyncio.run(run())


def test_serial_per_chat_parallel_across_chats_and_replay():
    async def run():
        db = Database()
        first_started = asyncio.Event()
        release_first = asyncio.Event()
        calls = []
        class Transport:
            async def send(self, **kwargs):
                text = kwargs['text']
                calls.append(text)
                if text == 'first':
                    first_started.set()
                    await release_first.wait()
                return 'om_' + text
        box = outbound.Outbox('test', Transport(), outbound.Gateway('test'), db)
        async def send(chat, text):
            return await box.send('send', chat, {'text': text}, None, text, 'answer')
        first = asyncio.create_task(send('same', 'first'))
        await first_started.wait()
        second = asyncio.create_task(send('same', 'second'))
        other = asyncio.create_task(send('other', 'other'))
        try:
            assert (await other).success
            assert calls == ['first', 'other']
            assert not second.done()
            release_first.set()
            assert all(r.success for r in await asyncio.gather(first, second))
            assert calls == ['first', 'other', 'second']
            assert (await send('same', 'first')).success
            assert calls == ['first', 'other', 'second']
            assert db.recovers == 1
        finally:
            release_first.set()
            await box.stop()
    asyncio.run(run())


def test_wait_deadline_does_not_cancel_delivery(monkeypatch):
    monkeypatch.setattr(outbound, 'SEND_WAIT_SECONDS', .02)
    async def run():
        db = Database()
        release = asyncio.Event()
        class Transport:
            async def send(self, **kwargs):
                await release.wait()
                return 'om_sent'
        box = outbound.Outbox('test', Transport(), outbound.Gateway('test'), db)
        try:
            receipt = await box.send('send', 'chat', {'text': 'hello'}, None, 'key', 'answer')
            assert receipt.result == PostOutcome.UNKNOWN
            assert db.rows[1]['state'] == 'sending'
            release.set()
            await until(lambda: db.rows[1]['state'] == 'sent')
            assert db.rows[1]['external_id'] == 'om_sent'
        finally:
            await box.stop()
    asyncio.run(run())


def test_open_breaker_defers_pending_without_consuming_attempts(monkeypatch):
    monkeypatch.setattr(outbound, 'SEND_WAIT_SECONDS', .02)
    async def run():
        db = Database()
        gateway = outbound.Gateway('test')
        gateway.breaker.state = 'open'
        gateway.breaker.open_until = gateway.clock() + .15
        calls = []
        class Transport:
            async def send(self, **kwargs):
                calls.append(kwargs['text'])
                return 'om_sent'
        box = outbound.Outbox('test', Transport(), gateway, db)
        try:
            receipts = await asyncio.gather(*[
                box.send('send', 'chat', {'text': str(i)}, None, str(i), 'answer') for i in range(2)])
            assert all(r.result == PostOutcome.UNKNOWN for r in receipts)
            assert db.recovers == 1
            assert calls == []
            assert all(row['state'] == 'pending' and row['attempts'] == 0 for row in db.rows.values())
            assert db.rows[1]['next_attempt_at'] > datetime.now(timezone.utc)
            await until(lambda: all(row['state'] == 'sent' for row in db.rows.values()))
            assert calls == ['0', '1']
            assert gateway.breaker.state == 'closed'
        finally:
            await box.stop()
    asyncio.run(run())


def test_switch_off_does_not_pause_persisted_work():
    async def run():
        db = Database()
        enabled = True
        calls = []
        class Transport:
            async def send(self, **kwargs):
                calls.append(kwargs['text'])
                return 'om_sent'
        await db.insert_or_get(dict(channel_id='test', chat='chat', kind='send', body={'text': 'hello'},
                                    reply_to=None, idempotency_key='key', source='answer'))
        box = outbound.Outbox('test', Transport(), outbound.Gateway('test'), db, enabled=lambda: enabled)
        await box.start()
        enabled = False
        try:
            await until(lambda: db.rows[1]['state'] == 'sent')
            scans = db.scans
            await asyncio.sleep(.15)
            assert db.scans == scans
            assert calls == ['hello']
        finally:
            await box.stop()
    asyncio.run(run())


def test_running_breaker_returns_unattempted_claims_without_spending_retries():
    async def run():
        db = Database()
        gateway = outbound.Gateway('test')
        # Queue a claimed row behind an exhausted bucket, then open the circuit
        # before the gateway gets its token: no platform call has occurred.
        gateway.bucket.tokens = 0
        gateway.bucket.updated = gateway.clock() + .05
        calls = []
        class Transport:
            async def send(self, **kwargs):
                calls.append(kwargs['text'])
                return 'om_sent'
        row = await db.insert_or_get(dict(channel_id='test', chat='other', kind='send', body={'text': 'queued'},
                                          reply_to=None, idempotency_key='key', source='answer'))
        await db.mark_sending(row['id'])
        row['attempts'] = 1
        box = outbound.Outbox('test', Transport(), gateway, db)
        task = asyncio.create_task(box._deliver(row))
        await asyncio.sleep(.01)
        for _ in range(5):
            gateway.breaker.record(outbound.ErrorClass.UNKNOWN)
        await task
        assert calls == []
        assert db.rows[1]['state'] == 'pending'
        assert db.rows[1]['attempts'] == 0
        assert db.rows[1].get('error_class') is None
        assert db.rows[1]['next_attempt_at'] > datetime.now(timezone.utc)
    asyncio.run(run())
