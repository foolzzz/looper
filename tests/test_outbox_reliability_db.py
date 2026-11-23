"""Fault recovery must never trade a lost acknowledgement for a duplicate send."""
import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest

from tests.db_support import DSN, run_db

pytestmark = pytest.mark.skipif(not DSN, reason='disposable PostgreSQL required')


@pytest.mark.parametrize('restart', [False, True])
def test_expired_unknown_pending_is_never_retried(monkeypatch, restart):
    from looper.agents.channels import outbound
    from looper.db import channel_outbox as db

    async def check(pool):
        now = datetime.now(timezone.utc) + timedelta(seconds=1)
        transport = AsyncMock()
        transport.send.side_effect = outbound.LarkCliError('response lost', error_type=outbound.TIMEOUT)
        box = outbound.Outbox('app', transport, outbound.Gateway('app'), clock=lambda: now)
        monkeypatch.setattr(outbound, 'SEND_WAIT_SECONDS', .1)
        monkeypatch.setattr(outbound, 'DELIVERY_DEADLINE', 3 * outbound.KEYED_DEDUPE_WINDOW)
        monkeypatch.setattr(outbound, 'backoff', lambda *args: 10)
        try:
            await box.send('send', 'chat', {'text': 'hello'}, None, 'stable-key', 'answer')
            original = await pool.fetchrow('SELECT * FROM channel_outbox')
            assert original['state'] == 'pending' and original['error_class'] == 'unknown'
            # Moving the clock across the dedupe boundary must also protect pending rows.
            now += timedelta(hours=2)
            transport.send.side_effect = None
            transport.send.return_value = 'duplicate'
            if restart:
                await box.stop()
                await box.start()
            else:
                box._wake.set()
            receipt = await box.send('send', 'chat', {'text': 'hello'}, None, 'stable-key', 'answer')
            current = await db.get(original['id'])
            assert current['state'] == 'uncertain'
            assert not receipt.success
            assert transport.send.await_count == 1
            assert current['first_possible_at'] == original['first_possible_at']
        finally:
            await box.stop()
    run_db(check, monkeypatch, timeout=5)


@pytest.mark.parametrize('committed', [False, True])
def test_sent_state_failure_retries_only_persistence_and_releases_chat(monkeypatch, committed):
    from looper.agents.channels import outbound
    from looper.db import channel_outbox as db

    async def check(pool):
        transport = AsyncMock()
        transport.send.return_value = 'om_sent'
        original = db.mark_sent
        writes = 0
        async def fail_once(*args, **kwargs):
            nonlocal writes
            writes += 1
            if writes == 1:
                if committed:
                    await original(*args, **kwargs)
                raise ConnectionError('temporary state write failure')
            await original(*args, **kwargs)
        monkeypatch.setattr(db, 'mark_sent', fail_once)
        box = outbound.Outbox('app', transport, outbound.Gateway('app'))
        try:
            first = asyncio.create_task(box.send('send', 'chat', {'text': 'one'}, None, 'one', 'answer'))
            second = asyncio.create_task(box.send('send', 'chat', {'text': 'two'}, None, 'two', 'answer'))
            receipts = await asyncio.wait_for(asyncio.gather(first, second), 3)
            assert all(r.success for r in receipts)
            assert transport.send.await_count == 2
            assert writes == 3
            assert await db.counts('app') == dict(pending=0, sending=0, sent=2, failed=0, uncertain=0)
        finally:
            await box.stop()
    run_db(check, monkeypatch, timeout=6)


def test_total_deadline_releases_unsent_chat_head(monkeypatch):
    from looper.agents.channels import outbound
    from looper.db import channel_outbox as db

    async def check(pool):
        now = datetime.now(timezone.utc) + timedelta(seconds=1)
        item = await db.insert_or_get(dict(channel_id='app', idempotency_key='old', kind='send',
                                          chat='chat', body={'text': 'old'}, source='answer'))
        await pool.execute('UPDATE channel_outbox SET created_at=$1, next_attempt_at=$2 WHERE id=$3',
                           now - timedelta(days=1), now + timedelta(days=1000), item['id'])
        transport = AsyncMock()
        transport.send.return_value = 'om_new'
        box = outbound.Outbox('app', transport, outbound.Gateway('app'), clock=lambda: now)
        try:
            result = await box.send('send', 'chat', {'text': 'new'}, None, 'new', 'answer')
            assert result.success
            assert (await db.get(item['id']))['state'] == 'failed'
            assert transport.send.await_count == 1
        finally:
            await box.stop()
    run_db(check, monkeypatch, timeout=5)


def test_lost_claim_acknowledgement_does_not_strand_chat(monkeypatch):
    from looper.agents.channels import outbound
    from looper.db import channel_outbox as db

    async def check(pool):
        original = db.mark_sending
        claims = 0
        async def lose_ack(*args, **kwargs):
            nonlocal claims
            claims += 1
            result = await original(*args, **kwargs)
            if claims == 1:
                raise ConnectionError('claim committed, acknowledgement lost')
            return result
        monkeypatch.setattr(db, 'mark_sending', lose_ack)
        transport = AsyncMock()
        transport.send.return_value = 'om_sent'
        box = outbound.Outbox('app', transport, outbound.Gateway('app'))
        try:
            result = await asyncio.wait_for(box.send('send', 'chat', {'text': 'one'}, None, 'one', 'answer'), 3)
            assert result.success
            assert transport.send.await_count == 1
            item, = await pool.fetch('SELECT * FROM channel_outbox')
            assert item['state'] == 'sent' and item['attempts'] == 1
        finally:
            await box.stop()
    run_db(check, monkeypatch, timeout=5)


def test_timestamp_write_wait_cannot_cross_dedupe_window(monkeypatch):
    from looper.agents.channels import outbound
    from looper.db import channel_outbox as db

    async def check(pool):
        now = datetime.now(timezone.utc) + timedelta(seconds=1)
        monkeypatch.setattr(outbound, 'DELIVERY_DEADLINE', 3 * outbound.KEYED_DEDUPE_WINDOW)
        item = await db.insert_or_get(dict(channel_id='app', idempotency_key='waiting', kind='send',
                                          chat='chat', body={'text': 'hello'}, source='answer'))
        await db.mark_sending(item['id'])
        await db.mark_attempted(item['id'], now-timedelta(seconds=outbound.KEYED_DEDUPE_WINDOW-1))
        await db.retry_later(item['id'], now, 'unknown', 'response lost')
        original = db.mark_attempted
        async def slow_write(*args):
            nonlocal now
            result = await original(*args)
            now += timedelta(seconds=2)
            return result
        monkeypatch.setattr(db, 'mark_attempted', slow_write)
        transport = AsyncMock()
        box = outbound.Outbox('app', transport, outbound.Gateway('app'), clock=lambda: now)
        try:
            result = await box.send('send', 'chat', {'text': 'hello'}, None, 'waiting', 'answer')
            assert not result.success
            assert (await db.get(item['id']))['state'] == 'uncertain'
            transport.send.assert_not_awaited()
        finally:
            await box.stop()
    run_db(check, monkeypatch, timeout=5)


@pytest.mark.parametrize('failure', ['hang', 'errors', 'lost_ack'])
def test_attempt_write_deadline_releases_app_slot_and_defers_without_sending(monkeypatch, failure):
    from looper.agents.channels import outbound
    from looper.db import channel_outbox as db

    async def check(pool):
        monkeypatch.setattr(outbound, 'STATE_WRITE_TIMEOUT', .05)
        monkeypatch.setattr(outbound, 'MAX_BACKOFF', .01)
        def limits():
            return dict(send_qps=100, write_qps=100, max_in_flight=1, edit_interval_seconds=0)
        gateway = outbound.Gateway('app', settings=limits, app_id='synthetic-deadline')
        sibling = outbound.Gateway('bot', settings=limits, app_id='synthetic-deadline')
        transport = AsyncMock()
        transport.send.return_value = 'om_sent'
        box = outbound.Outbox('app', transport, gateway)
        row = await db.insert_or_get(dict(channel_id='app', idempotency_key='same-key', kind='send',
                                         chat='chat', body={'text': 'hello'}, source='answer'))
        await db.mark_sending(row['id'])
        row['attempts'] = 1
        writing, db_available, deferring = asyncio.Event(), asyncio.Event(), asyncio.Event()
        original_attempt, original_defer = db.mark_attempted, db.defer_unsent
        async def unavailable(*args):
            writing.set()
            if failure == 'lost_ack':
                await original_attempt(*args)
            if failure == 'hang':
                await db_available.wait()
            raise ConnectionError('synthetic database unavailable')
        async def defer(*args):
            deferring.set()
            await db_available.wait()
            return await original_defer(*args)
        monkeypatch.setattr(db, 'mark_attempted', unavailable)
        monkeypatch.setattr(db, 'defer_unsent', defer)
        worker = asyncio.create_task(box._deliver(row))
        try:
            await asyncio.wait_for(writing.wait(), 1)
            # Even the defer write is blocked: a DB-free write on the sibling must proceed.
            reaction = AsyncMock(return_value='ok')
            assert await asyncio.wait_for(sibling.write('react', reaction), .3) == 'ok'
            await asyncio.wait_for(deferring.wait(), .3)
            assert gateway.breaker.state == 'closed'
            assert not gateway.counters.failures
            transport.send.assert_not_awaited()
            db_available.set()
            await asyncio.wait_for(worker, 1)
            current = await db.get(row['id'])
            assert current['state'] == 'pending' and current['attempts'] == 0
            assert current['next_attempt_at'] > current['created_at']
            monkeypatch.setattr(db, 'mark_attempted', original_attempt)
            receipt = await asyncio.wait_for(box.send('send', 'chat', {'text': 'hello'}, None,
                                                     'same-key', 'answer'), 2)
            assert receipt.success and transport.send.await_count == 1
            assert transport.send.call_args.kwargs['idempotency_key'] == 'same-key'
            if failure == 'lost_ack':
                assert (await db.get(row['id']))['first_possible_at'] == current['first_possible_at']
        finally:
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)
            await box.stop()
    run_db(check, monkeypatch, timeout=5)
