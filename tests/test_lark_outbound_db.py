"""Durable Lark writes against PostgreSQL and the CLI stand-in only."""
import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from tests.db_support import DSN, run_db
from lark_cli_stand_in import OPEN_ID

pytestmark = pytest.mark.skipif(not DSN, reason='disposable PostgreSQL required')


def test_cli_deadline_after_delivery_retries_the_same_key_once(monkeypatch, lark_cli_stand_in):
    from looper.agents import chat_settings
    from looper.agents.channels import lark_cli
    from looper.agents.channels.allowlist import SenderAllowlist
    from looper.agents.channels.lark import LarkChannelAdapter
    from looper.agents.channels.outbound import ErrorClass

    async def check(pool):
        monkeypatch.setattr(lark_cli, 'CALL_TIMEOUT_SECONDS', 1)
        monkeypatch.setattr(chat_settings, '_outbound_enabled', True)
        adapter = LarkChannelAdapter(allowlist=SenderAllowlist('lark', frozenset([OPEN_ID])))
        started = asyncio.get_running_loop().time()
        try:
            # The stand-in delivers, then hangs: exercise the real subprocess deadline with an accelerated timeout.
            receipt = await adapter._send_message('oc_synthetic_hangx1', 'answer')
            assert receipt.success
            assert asyncio.get_running_loop().time() - started >= lark_cli.CALL_TIMEOUT_SECONDS
            row, = await pool.fetch('SELECT * FROM channel_outbox')
            assert row['state'] == 'sent' and row['attempts'] == 2
            assert adapter._gateway.counters.failures == {ErrorClass.UNKNOWN: 1}
            assert adapter._gateway.counters.backoffs == 1
            calls = [json.loads(line) for line in lark_cli_stand_in.read_text().splitlines()]
            sends = [c for c in calls if '+messages-send' in c]
            assert len(sends) == 2
            assert [c[c.index('--idempotency-key') + 1] for c in sends] == [row['idempotency_key']] * 2
            journal = Path(str(lark_cli_stand_in) + '.delivered.jsonl')
            delivered = [json.loads(line) for line in journal.read_text().splitlines()]
            assert len(delivered) == 1
            assert delivered[0]['key'] == row['idempotency_key']
            assert row['external_id'] == delivered[0]['message_id']
        finally:
            await adapter._channel_outbox.stop()
    run_db(check, monkeypatch, timeout=45)


@pytest.mark.parametrize(('suffix', 'count'), [('tox2', 5), ('rl230020x3', 10), ('rl99991400x2', 3)])
def test_lark_keyed_delivery_and_replay(monkeypatch, lark_cli_stand_in: Path, suffix, count):
    from looper.agents import chat_settings
    from looper.agents.channels.allowlist import SenderAllowlist
    from looper.agents.channels.lark import LarkChannelAdapter
    from looper.agents.channels.outbound import Gateway
    from looper.agents.message import MessageEnvelope, MessageType, MANAGER_ADDRESS, channel_address

    async def check(pool):
        monkeypatch.setattr(chat_settings, '_outbound_enabled', True)
        adapter = LarkChannelAdapter(allowlist=SenderAllowlist('lark', frozenset([OPEN_ID])))
        async def fast_sleep(_):
            await asyncio.sleep(0)
        adapter._gateway.sleep = fast_sleep
        # The persisted retry deadline also uses backoff; its schedule is covered with a fake clock.
        monkeypatch.setattr('looper.agents.channels.outbound.backoff', lambda *args: 0)
        adapter._gateway = Gateway(adapter.channel_id, sleep=fast_sleep)
        adapter._channel_outbox.gateway = adapter._gateway
        chat = 'oc_synthetic_' + suffix
        envelopes = [MessageEnvelope(str(i), 'corr', '', MANAGER_ADDRESS, channel_address('lark'),
                                     MessageType.AGENT_REPLY, {'text': f'answer {i}'}) for i in range(count)]
        adapter._correlation_chat['corr'] = chat
        try:
            receipts = await asyncio.gather(*(adapter.deliver(e) for e in envelopes))
            assert all(r.success for r in receipts)
            assert (await adapter.deliver(envelopes[0])).success
            rows = await pool.fetch('SELECT * FROM channel_outbox ORDER BY id')
            assert len(rows) == count
            assert all(r['state'] == 'sent' for r in rows)
            assert len({r['external_id'] for r in rows}) == count
            assert all(r['source'] == 'answer' for r in rows)
            calls = [json.loads(line) for line in lark_cli_stand_in.read_text().splitlines()]
            sends = [c for c in calls if '+messages-send' in c]
            assert all('--idempotency-key' in c for c in sends)
            assert len({c[c.index('--idempotency-key') + 1] for c in sends}) == count
            delivered = [json.loads(line) for line in Path(str(lark_cli_stand_in) + '.delivered.jsonl').read_text().splitlines()]
            assert len(delivered) == count
            assert len({d['key'] for d in delivered}) == count
        finally:
            await adapter._channel_outbox.stop()
    run_db(check, monkeypatch)


def test_crash_recovers_keyed_send_without_new_message(monkeypatch, lark_cli_stand_in):
    from looper.agents.channels.lark_transport import CliTransport
    from looper.agents.channels.outbound import Gateway, Outbox
    from looper.db import channel_outbox

    async def check(pool):
        transport = CliTransport()
        first = await transport.send(chat_id='oc_synthetic', text='hello', idempotency_key='crash', return_id=True)
        row = await channel_outbox.insert_or_get(dict(channel_id='channel-lark', idempotency_key='crash',
            kind='send', chat='oc_synthetic', body={'text': 'hello'}, source='answer'))
        await channel_outbox.mark_sending(row['id'])
        await channel_outbox.mark_attempted(row['id'], datetime.now(timezone.utc))
        old = await channel_outbox.insert_or_get(dict(channel_id='channel-lark', idempotency_key='old',
            kind='send', chat='oc_old', body={'text': 'old'}, source='answer'))
        await channel_outbox.mark_sending(old['id'])
        await channel_outbox.mark_attempted(old['id'], datetime.now(timezone.utc))
        await pool.execute("UPDATE channel_outbox SET created_at=now()-interval '2 hours', first_possible_at=now()-interval '2 hours' WHERE id=$1", old['id'])
        outbox = Outbox('channel-lark', transport, Gateway('channel-lark'), channel_outbox)
        try:
            await outbox.start()
            receipt = await outbox.send('send', 'oc_synthetic', {'text': 'hello'}, None, 'crash', 'answer')
            assert receipt.success and receipt.external_ids == (first,)
            assert (await channel_outbox.get(old['id']))['state'] == 'uncertain'
        finally:
            await outbox.stop()
    run_db(check, monkeypatch)


def test_disabled_transport_calls_are_pinned(monkeypatch, lark_cli_stand_in):
    from looper.agents import chat_settings
    from looper.agents.channels.allowlist import SenderAllowlist
    from looper.agents.channels.lark import LarkChannelAdapter
    from looper.agents.task_notice import NotifyTarget, TargetKind

    async def check(pool):
        monkeypatch.setattr(chat_settings, '_outbound_enabled', False)
        monkeypatch.setattr('looper.agents.channels.lark.split_message', lambda _: ['notice 1', 'notice 2'])
        adapter = LarkChannelAdapter(allowlist=SenderAllowlist('lark', frozenset([OPEN_ID])))
        await adapter._send_message('oc_synthetic', 'answer')
        await adapter._send_notice(NotifyTarget(TargetKind.CHAT, 'oc_synthetic', group=True), 'notice')
        await adapter.post(NotifyTarget(TargetKind.CHAT, 'oc_synthetic'), 'post', idempotency_key='caller-key')
        await adapter._edit_status('oc_synthetic', 'om_status', 'status')
        await adapter._react('oc_synthetic', 'om_original')
        calls = [json.loads(line) for line in lark_cli_stand_in.read_text().splitlines()]
        assert calls == [
            ['im', '+messages-send', '--as', 'bot', '--chat-id', 'oc_synthetic', '--markdown', 'answer'],
            ['im', '+messages-send', '--as', 'bot', '--chat-id', 'oc_synthetic', '--markdown', 'notice 1'],
            ['im', '+messages-send', '--as', 'bot', '--chat-id', 'oc_synthetic', '--markdown', 'notice 2'],
            ['im', '+messages-send', '--as', 'bot', '--chat-id', 'oc_synthetic', '--markdown', 'post', '--idempotency-key', 'caller-key'],
            ['im', '+messages-edit', '--as', 'bot', '--message-id', 'om_status', '--text', 'status'],
            ['im', 'reactions', 'create', '--as', 'bot', '--message-id', 'om_original', '--data',
             '{"reaction_type": {"emoji_type": "Get"}}'],
        ]
        assert await pool.fetchval('SELECT count(*) FROM channel_outbox') == 0
    run_db(check, monkeypatch)


def test_sources_threaded_posts_and_ephemeral_status(monkeypatch, lark_cli_stand_in):
    from looper.agents import chat_settings
    from looper.agents.channels.allowlist import SenderAllowlist
    from looper.agents.channels.lark import LarkChannelAdapter
    from looper.agents.message import MessageEnvelope, MessageType, MANAGER_ADDRESS, channel_address
    from looper.agents.task_notice import NotifyTarget, TargetKind

    async def check(pool):
        monkeypatch.setattr(chat_settings, '_outbound_enabled', True)
        adapter = LarkChannelAdapter(allowlist=SenderAllowlist('lark', frozenset([OPEN_ID])))
        adapter._correlation_chat['corr'] = 'oc_synthetic'
        adapter._group_message['corr'] = 'om_trigger'
        adapter._thread_messages['om_trigger'] = None
        try:
            reply = MessageEnvelope('reply', 'corr', '', MANAGER_ADDRESS, channel_address('lark'),
                                    MessageType.AGENT_REPLY, {'text': 'answer'})
            assert (await adapter.deliver(reply)).success
            assert (await adapter.notify('oc_synthetic', 'notification')).success
            assert (await adapter.post(NotifyTarget(TargetKind.USER, OPEN_ID), 'post',
                                       idempotency_key='supplied')).success
            progress = MessageEnvelope('progress', 'corr', '', MANAGER_ADDRESS, channel_address('lark'),
                                       MessageType.STATUS_UPDATE, {'text': 'progress'})
            assert (await adapter.deliver(progress)).success
            notice = MessageEnvelope('notice', 'corr', '', MANAGER_ADDRESS, channel_address('lark'),
                                     MessageType.CHAT_NOTICE, {'channel': 'lark', 'channel_id': 'channel-lark',
                                     'chat': 'oc_synthetic', 'kind': 'chat', 'group': False, 'text': 'notice'})
            assert (await adapter.deliver(notice)).success
            await adapter._send_status('corr', 'status')
            rows = await pool.fetch('SELECT * FROM channel_outbox ORDER BY id')
            assert [r['source'] for r in rows] == ['answer', 'notify', 'post', 'progress', 'notice']
            assert rows[0]['kind'] == 'reply' and rows[0]['reply_to'] == 'om_trigger'
            assert json.loads(rows[0]['body'])['reply_in_thread'] is True
            assert rows[2]['idempotency_key'] == 'supplied'
            assert json.loads(rows[2]['body'])['user_id'] == OPEN_ID
            journal = Path(str(lark_cli_stand_in) + '.delivered.jsonl')
            assert all(json.loads(line)['key'] for line in journal.read_text().splitlines())
        finally:
            await adapter._channel_outbox.stop()
    run_db(check, monkeypatch)
