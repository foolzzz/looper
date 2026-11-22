import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest

from tests.db_support import DSN, run_db

pytestmark = pytest.mark.skipif(not DSN, reason='disposable PostgreSQL required')


def test_switch_off_still_drains_pending_and_retries(monkeypatch):
    from looper.agents.channels import outbound

    async def check(pool):
        enabled = True
        calls = []
        first = asyncio.Event()
        release = asyncio.Event()
        class Transport:
            async def send(self, **body):
                calls.append(body['idempotency_key'])
                first.set()
                await release.wait()
                if len(calls) == 1:
                    raise outbound.LarkCliError('busy', code=230020)
                return 'om_sent'
        monkeypatch.setattr(outbound, 'backoff', lambda *args: .05)
        outbox = outbound.Outbox('app', Transport(), outbound.Gateway('app'), enabled=lambda: enabled)
        try:
            sending = asyncio.create_task(outbox.send('send', 'chat', {'text': 'one'}, None, 'one', 'answer'))
            await first.wait()
            second = asyncio.create_task(outbox.send('send', 'chat', {'text': 'two'}, None, 'two', 'answer'))
            await asyncio.sleep(.02)
            enabled = False
            release.set()
            receipts = await asyncio.wait_for(asyncio.gather(sending, second), 2)
            assert all(receipt.success for receipt in receipts)
            assert calls == ['one', 'one', 'two']
        finally:
            await outbox.stop()
    run_db(check, monkeypatch)


def test_disabled_boot_recovers_leftovers_and_empty_boot_does_not_write(monkeypatch):
    from looper.agents.channels.outbound import Gateway, Outbox
    from looper.db import channel_outbox as db
    async def check(pool):
        transport = AsyncMock()
        transport.send.return_value = 'om_sent'
        outbox = Outbox('app', transport, Gateway('app'), enabled=lambda: False)
        await outbox.start_if_needed()
        await outbox._recovery_task
        assert outbox._task is None
        row = await db.insert_or_get(dict(channel_id='app', idempotency_key='leftover', kind='send',
                                         chat='chat', body={'text': 'hello'}, source='answer'))
        await db.mark_sending(row['id'])
        try:
            await outbox.start_if_needed()
            receipt = await outbox.send('send', 'chat', {'text': 'hello'}, None, 'leftover', 'answer')
            assert receipt.success
            assert transport.send.call_args.kwargs['idempotency_key'] == 'leftover'
        finally:
            await outbox.stop()
    run_db(check, monkeypatch)


def test_disabled_boot_recovers_after_the_pool_becomes_available(monkeypatch):
    from looper.agents.channels.outbound import Gateway, Outbox
    from looper.db import channel_outbox as db

    async def check(pool):
        row = await db.insert_or_get(dict(channel_id='app', idempotency_key='leftover', kind='send',
                                         chat='chat', body={'text': 'hello'}, source='answer'))
        await db.mark_sending(row['id'])
        transport = AsyncMock()
        transport.send.return_value = 'om_recovered'
        outbox = Outbox('app', transport, Gateway('app'), enabled=lambda: False)
        try:
            async with pool.acquire():
                await asyncio.wait_for(outbox.start_if_needed(), .05)
                # Recovery gets more than the old 100 ms budget without blocking startup.
                await asyncio.sleep(.15)
                transport.send.assert_not_awaited()
            async def delivered():
                while (await db.get(row['id']))['state'] != 'sent':
                    await asyncio.sleep(.01)
            await asyncio.wait_for(delivered(), 2)
            assert transport.send.await_count == 1
            assert transport.send.call_args.kwargs['idempotency_key'] == 'leftover'
        finally:
            await outbox.stop()
    run_db(check, monkeypatch, max_size=1, timeout=5)


def test_waiters_and_idle_scanner_do_not_poll(monkeypatch):
    from looper.agents.channels.outbound import Gateway, Outbox
    from looper.db import channel_outbox as db
    async def check(pool):
        transport = AsyncMock()
        transport.send.return_value = 'om_sent'
        row = await db.insert_or_get(dict(channel_id='app', idempotency_key='later', kind='send',
                                         chat='chat', body={'text': 'hello'}, source='answer'))
        await db.retry_later(row['id'], datetime.now(timezone.utc) + timedelta(seconds=.5), 'transient', 'retry')
        get = AsyncMock(wraps=db.get)
        scan = AsyncMock(wraps=db.next_due)
        monkeypatch.setattr(db, 'get', get)
        monkeypatch.setattr(db, 'next_due', scan)
        outbox = Outbox('app', transport, Gateway('app'))
        idle = asyncio.Event()
        wait = outbox._wake.wait
        async def wait_for_work():
            if not outbox._workers and not outbox._wake.is_set():
                idle.set()
            await wait()
        monkeypatch.setattr(outbox._wake, 'wait', wait_for_work)
        try:
            receipts = await asyncio.gather(*(outbox.send('send', 'chat', {'text': 'hello'}, None,
                                                         'later', 'answer') for _ in range(20)))
            assert all(r.success for r in receipts)
            assert get.call_count <= 21
            # Concurrent registrations may wake scans while work is arriving. Once idle,
            # the scanner must sleep until new work arrives, regardless of database timing.
            idle.clear()
            outbox._wake.set()
            await asyncio.wait_for(idle.wait(), 2)
            scans = scan.call_count
            await asyncio.sleep(.3)
            assert scan.call_count == scans
        finally:
            await outbox.stop()
    run_db(check, monkeypatch)


def test_twenty_chats_outbox_never_exceeds_four_in_flight(monkeypatch):
    from looper.agents.channels.outbound import Gateway, Outbox
    async def check(pool):
        active = peak = 0
        class Transport:
            async def send(self, **body):
                nonlocal active, peak
                active += 1
                peak = max(peak, active)
                await asyncio.sleep(.05)
                active -= 1
                return 'om_' + body['chat_id']
        outbox = Outbox('app', Transport(), Gateway('app'))
        try:
            receipts = await asyncio.gather(*(outbox.send('send', f'chat_{i}', {'text': 'hello'}, None,
                                                         f'key_{i}', 'answer') for i in range(20)))
            assert all(r.success for r in receipts)
            assert 1 < peak <= 4
        finally:
            await outbox.stop()
    run_db(check, monkeypatch)
