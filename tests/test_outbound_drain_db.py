import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest

from tests.db_support import DSN, run_db

pytestmark = pytest.mark.skipif(not DSN, reason='disposable PostgreSQL required')


@pytest.mark.parametrize('wake_on_recovery', [True, False])
def test_sibling_outbox_holds_sends_and_wakes_after_another_channels_probe(monkeypatch, wake_on_recovery):
    from looper.agents.channels import outbound
    from looper.db import channel_outbox as db

    async def check(pool):
        now = [0.0]
        first, sibling = [outbound.Gateway(channel, clock=lambda: now[0], app_id='synthetic-app')
                          for channel in ('first', 'sibling')]

        async def failure():
            raise outbound.LarkCliError('lost response', error_type=outbound.TIMEOUT)

        for _ in range(5):
            with pytest.raises(outbound.LarkCliError):
                await first.write('edit', failure, retry=False)
        row = await db.insert_or_get(dict(channel_id='sibling', idempotency_key='held', kind='send',
            chat='synthetic', body={'text': 'held'}, source='answer'))
        transport = AsyncMock()
        transport.send.return_value = 'om_recovered'
        box = outbound.Outbox('sibling', transport, sibling)
        deferred, parked, probing, release, saved = [asyncio.Event() for _ in range(5)]
        defer, mark_sent, wait = db.defer_unsent, db.mark_sent, box._wake.wait

        async def defer_unsent(*args):
            await defer(*args)
            deferred.set()

        async def sent(*args):
            await mark_sent(*args)
            saved.set()

        async def wait_for_work():
            if sibling.breaker.state == 'half_open' and not box._wake.is_set():
                parked.set()
            await wait()

        async def probe():
            probing.set()
            await release.wait()
            return 'om_probe'

        monkeypatch.setattr(db, 'defer_unsent', defer_unsent)
        monkeypatch.setattr(db, 'mark_sent', sent)
        monkeypatch.setattr(box._wake, 'wait', wait_for_work)
        task = None
        try:
            await box.start()
            await asyncio.wait_for(deferred.wait(), 2)
            held = await db.get(row['id'])
            assert held['state'] == 'pending' and held['attempts'] == 0
            transport.send.assert_not_awaited()
            now[0] = 60
            task = asyncio.create_task(first.write('send', probe))
            await asyncio.wait_for(probing.wait(), 2)
            await pool.execute('UPDATE channel_outbox SET next_attempt_at=now() WHERE id=$1', row['id'])
            box._wake.set()
            await asyncio.wait_for(parked.wait(), 2)
            if not wake_on_recovery:
                # Recovery must still be observed if the transition notification is lost.
                monkeypatch.setattr(first.breaker, '_on_transition', None)
            release.set()
            assert await asyncio.wait_for(task, 2) == 'om_probe'
            await asyncio.wait_for(saved.wait(), 2)
            delivered = await db.get(row['id'])
            assert delivered['state'] == 'sent' and delivered['attempts'] == 1
            assert transport.send.await_count == 1
        finally:
            release.set()
            if task is not None:
                await asyncio.gather(task, return_exceptions=True)
            await box.stop()
    run_db(check, monkeypatch)


def test_stopping_probing_channel_releases_sibling_outbox(monkeypatch):
    from looper.agents import chat_settings
    from looper.agents.channels import outbound
    from looper.agents.channels.allowlist import SenderAllowlist
    from looper.agents.channels.lark import LarkChannelAdapter
    from looper.agents.channels.lark_transport import LarkTransportKind
    from looper.db import channel_outbox as db

    async def check(pool):
        monkeypatch.setattr(chat_settings, '_outbound_enabled', True)
        probing, parked, sent = [asyncio.Event() for _ in range(3)]
        transport = AsyncMock()
        transport.kind = LarkTransportKind.BOT
        transport.consumer_started = None

        async def hold(**kwargs):
            probing.set()
            await asyncio.Event().wait()

        transport.send.side_effect = hold
        adapter = LarkChannelAdapter(bot_app_id='synthetic-stop-probe', transport=transport,
            allowlist=SenderAllowlist('channel-lark', frozenset({'ou_synthetic'})))
        gateway = adapter._gateway
        for _ in range(5):
            gateway.breaker.record(outbound.ErrorClass.UNKNOWN)
        gateway.breaker.open_until = gateway.clock()
        sibling = outbound.Gateway('sibling', app_id='synthetic-stop-probe')
        other_transport = AsyncMock()
        other_transport.send.return_value = 'om_sibling'
        box = outbound.Outbox('sibling', other_transport, sibling)
        wait, mark_sent = box._wake.wait, db.mark_sent

        async def waiting():
            if sibling.breaker.state == 'half_open' and not box._wake.is_set():
                parked.set()
            await wait()

        async def saved(*args):
            await mark_sent(*args)
            sent.set()

        monkeypatch.setattr(box._wake, 'wait', waiting)
        monkeypatch.setattr(db, 'mark_sent', saved)
        sending = asyncio.create_task(adapter._transport.send(chat_id='synthetic', text='probe'))
        try:
            await asyncio.wait_for(probing.wait(), 2)
            row = await db.insert_or_get(dict(channel_id='sibling', idempotency_key='sibling', kind='send',
                chat='synthetic', body={'text': 'after stop'}, source='answer'))
            await box.start()
            await asyncio.wait_for(parked.wait(), 2)
            await asyncio.wait_for(adapter.stop(), 2)
            await asyncio.wait_for(sent.wait(), 2)
            assert (await db.get(row['id']))['state'] == 'sent'
            assert other_transport.send.await_count == 1
            assert sibling.breaker.state == 'closed'
        finally:
            await adapter.stop()
            sending.cancel()
            await asyncio.gather(sending, return_exceptions=True)
            await box.stop()
    run_db(check, monkeypatch, timeout=10)


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
