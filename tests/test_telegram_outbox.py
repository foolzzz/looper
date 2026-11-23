"""Telegram persists each send without relying on platform idempotency."""
import asyncio
from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest

from tests.db_support import DSN, run_db

pytestmark = pytest.mark.skipif(not DSN, reason='disposable PostgreSQL required')


def test_telegram_attempt_delivers_once_and_replay_keeps_receipt(monkeypatch):
    from looper.agents.channels import outbound

    async def check(pool):
        transport = AsyncMock()
        transport.send.return_value = '41'
        box = outbound.Outbox('telegram-test', transport, outbound.Gateway('telegram-test'),
                              protocol=outbound.OutboundProtocol.TELEGRAM)
        try:
            first = await box.send('send', '123', {'text': 'hello'}, None, 'answer', 'answer')
            replay = await box.send('send', '123', {'text': 'hello'}, None, 'answer', 'answer')
            assert first.success and replay == first
            assert first.external_ids == ('41',)
            assert transport.send.await_count == 1
        finally:
            await box.stop()
    run_db(check, monkeypatch, timeout=5)


@pytest.mark.parametrize('attempted', [False, True])
def test_telegram_restart_never_retries_a_sending_row(monkeypatch, attempted):
    from looper.agents.channels import outbound
    from looper.db import channel_outbox as db

    async def check(pool):
        row = await db.insert_or_get(dict(channel_id='telegram-test', idempotency_key='interrupted',
                                         kind='send', chat='123', body={'text': 'hello'}, source='answer'))
        await db.mark_sending(row['id'])
        if attempted:
            await db.mark_attempted(row['id'], datetime.now(timezone.utc))
        transport = AsyncMock()
        box = outbound.Outbox('telegram-test', transport, outbound.Gateway('telegram-test'),
                              protocol=outbound.OutboundProtocol.TELEGRAM)
        try:
            await box.start()
            assert (await db.get(row['id']))['state'] == 'uncertain'
            receipt = await box.send('send', '123', {'text': 'hello'}, None, 'interrupted', 'answer')
            assert receipt.result == outbound.PostOutcome.UNKNOWN
            assert (await db.get(row['id']))['state'] == 'uncertain'
            transport.send.assert_not_awaited()
        finally:
            await box.stop()
    run_db(check, monkeypatch, timeout=5)


def test_definite_failure_can_retry_without_becoming_uncertain(monkeypatch):
    from looper.agents.channels import outbound

    async def check(pool):
        transport = AsyncMock()
        transport.send.side_effect = [ConnectionError('not connected'), '42']
        original = outbound.classify
        monkeypatch.setattr(outbound, 'classify', lambda exc: (outbound.ErrorClass.TRANSIENT, None)
                            if isinstance(exc, ConnectionError) else original(exc))
        monkeypatch.setattr(outbound, 'backoff', lambda *args: 0)
        box = outbound.Outbox('telegram-test', transport, outbound.Gateway('telegram-test'),
                              protocol=outbound.OutboundProtocol.TELEGRAM)
        try:
            receipt = await box.send('send', '123', {'text': 'hello'}, None, 'retry', 'notify')
            assert receipt.success and receipt.external_ids == ('42',)
            assert transport.send.await_count == 2
            row, = await pool.fetch('SELECT * FROM channel_outbox')
            assert row['state'] == 'sent' and row['attempts'] == 2
        finally:
            await box.stop()
    run_db(check, monkeypatch, timeout=5)


def test_rejected_body_replacement_is_atomic_and_keeps_the_row(monkeypatch):
    from looper.agents.channels import outbound
    from looper.db import channel_outbox as db

    async def check(pool):
        transport = AsyncMock()
        transport.send.side_effect = [outbound.LarkCliError('bad HTML', code=400), '43']
        box = outbound.Outbox('telegram-test', transport, outbound.Gateway('telegram-test'),
                              protocol=outbound.OutboundProtocol.TELEGRAM)
        try:
            rejected = await box.send('send', '123', {'text': '<bad>', 'parse_mode': 'HTML'}, None,
                                      'chunk', 'answer')
            assert rejected.result == outbound.PostOutcome.REJECTED
            before, = await pool.fetch('SELECT * FROM channel_outbox')
            receipts = await asyncio.gather(*[
                box.send('send', '123', {'text': 'plain'}, None, 'chunk', 'answer', replace_rejected=True)
                for _ in range(2)])
            assert all(receipt.success for receipt in receipts)
            current = await db.get(before['id'])
            assert current['state'] == 'sent' and current['body'] == {'text': 'plain'}
            assert await pool.fetchval('SELECT count(*) FROM channel_outbox') == 1
            assert transport.send.await_count == 2
        finally:
            await box.stop()
    run_db(check, monkeypatch, timeout=5)


@pytest.mark.parametrize('state', ['uncertain', 'sent', 'rejected', 'unknown'])
def test_body_replacement_never_reopens_possible_or_repeated_delivery(monkeypatch, state):
    from looper.agents.channels import outbound
    from looper.db import channel_outbox as db

    async def check(pool):
        row = await db.insert_or_get(dict(channel_id='telegram-test', idempotency_key='chunk', kind='send',
                                         chat='123', body={'text': 'plain'}, source='answer'))
        if state == 'uncertain':
            await db.mark_uncertain(row['id'], 'response lost')
        elif state == 'sent':
            await db.mark_sent(row['id'], '44')
        else:
            await db.mark_failed(row['id'], state, 'plain refused too')
        transport = AsyncMock()
        box = outbound.Outbox('telegram-test', transport, outbound.Gateway('telegram-test'),
                              protocol=outbound.OutboundProtocol.TELEGRAM)
        try:
            await box.send('send', '123', {'text': 'plain'}, None, 'chunk', 'answer', replace_rejected=True)
            assert (await db.get(row['id']))['state'] == (state if state in {'sent', 'uncertain'} else 'failed')
            transport.send.assert_not_awaited()
        finally:
            await box.stop()
    run_db(check, monkeypatch, timeout=5)


def test_rejected_replacement_does_not_send_without_database_proof(monkeypatch):
    from looper.agents.channels import outbound
    from looper.db import channel_outbox as db

    async def check(pool):
        monkeypatch.setattr(db, 'insert_or_get', AsyncMock(side_effect=ConnectionError('unavailable')))
        transport = AsyncMock()
        box = outbound.Outbox('telegram-test', transport, outbound.Gateway('telegram-test'),
                              protocol=outbound.OutboundProtocol.TELEGRAM)
        receipt = await box.send('send', '123', {'text': 'plain'}, None, 'chunk', 'answer', replace_rejected=True)
        assert receipt.result == outbound.PostOutcome.UNKNOWN
        transport.send.assert_not_awaited()
    run_db(check, monkeypatch, timeout=5)


def test_rejected_replacement_database_failure_returns_unknown(monkeypatch, caplog):
    from looper.agents.channels import outbound
    from looper.db import channel_outbox as db

    async def check(pool):
        item = await db.insert_or_get(dict(channel_id='telegram-test', idempotency_key='chunk', kind='send',
                                          chat='123', body={'text': '<bad>', 'parse_mode': 'HTML'}, source='answer'))
        await db.mark_failed(item['id'], 'rejected', 'bad HTML')
        monkeypatch.setattr(db, 'replace_rejected_body', AsyncMock(side_effect=ConnectionError('unavailable')))
        transport = AsyncMock()
        box = outbound.Outbox('telegram-test', transport, outbound.Gateway('telegram-test'),
                              protocol=outbound.OutboundProtocol.TELEGRAM)
        receipt = await box.send('send', '123', {'text': 'plain'}, None, 'chunk', 'answer', replace_rejected=True)
        assert receipt.result == outbound.PostOutcome.UNKNOWN
        assert 'cannot verify rejected delivery' in receipt.error
        assert 'outbound rejected body replacement failed' in caplog.text
        assert (await db.get(item['id']))['state'] == 'failed'
        transport.send.assert_not_awaited()
    run_db(check, monkeypatch, timeout=5)
