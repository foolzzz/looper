"""Telegram writes use a fake HTTP layer, including the real PTB lifecycle."""
import asyncio
import json
import time

import pytest

from looper.agents import chat_settings
from looper.agents.bus import MessageBus
from looper.agents.channels.allowlist import SenderAllowlist
from looper.agents.channels.outbound import Gateway, derived_key
from looper.agents.channels.telegram import TelegramChannelAdapter
from looper.agents.message import MANAGER_ADDRESS, MessageEnvelope, MessageType, channel_address
from looper.agents.task_notice import NotifyTarget, TargetKind
from tests.db_support import DSN, run_db


@pytest.fixture
def telegram_http(monkeypatch):
    from telegram.error import TimedOut
    from telegram.request import HTTPXRequest

    class API:
        def __init__(self):
            self.calls = []
            self.mode = None
            self.updates = []

        async def request(self, url, request_data=None, **kwargs):
            method = url.rsplit('/', 1)[-1]
            data = request_data.parameters if request_data else {}
            if method == 'getUpdates':
                await asyncio.sleep(.01)
                offset = data.get('offset', 0)
                result = [u for u in self.updates if u['update_id'] >= offset]
            elif method == 'getMe':
                result = {'id': 1, 'is_bot': True, 'first_name': 'Synthetic', 'username': 'synthetic_bot'}
            else:
                self.calls.append((method, data))
                if method == 'sendMessage':
                    if self.mode == 'timeout' or (self.mode == 'timeout_card' and data.get('reply_markup')):
                        raise TimedOut('synthetic lost response')
                    if self.mode == 'reject_card' and data.get('reply_markup'):
                        return 400, b'{"ok":false,"description":"Bad Request: invalid keyboard"}'
                    if self.mode == 'reject' and data.get('parse_mode') and data['text'].startswith('b'):
                        return 400, b'{"ok":false,"description":"Bad Request: invalid formatting"}'
                    result = {'message_id': len(self.calls), 'date': 1,
                              'chat': {'id': data['chat_id'], 'type': 'private'}, 'text': data['text']}
                else:
                    if method == 'editMessageText' and self.mode == 'reject':
                        return 403, b'{"ok":false,"description":"Forbidden"}'
                    result = True
            return 200, json.dumps({'ok': True, 'result': result}).encode()

    api = API()

    async def request(transport, **kwargs):
        return await api.request(**kwargs)

    monkeypatch.setattr(HTTPXRequest, 'do_request', request)
    return api


def adapter():
    return TelegramChannelAdapter(bot_token='0:synthetic', allowlist=SenderAllowlist(
        'channel-telegram', frozenset({'7'})))


def answer(text):
    return MessageEnvelope('answer-id', 'corr', '', MANAGER_ADDRESS, channel_address('telegram'),
                           MessageType.AGENT_REPLY, {'text': text},
                           metadata={'reply_route': {'chat_id': '7'}})


def test_request_retries_telegram_429_before_ptb_parsing():
    from telegram.request import RequestData
    from looper.agents.channels.telegram_request import GatedRequest

    async def run():
        now, waits, calls = 0., [], []

        async def sleep(delay):
            nonlocal now
            waits.append(delay)
            now += delay

        async def http(**kwargs):
            calls.append(kwargs)
            return ((429, b'{"ok":false,"parameters":{"retry_after":2}}') if len(calls) < 3
                    else (200, b'{"ok":true,"result":true}'))

        gateway = Gateway('channel-telegram', clock=lambda: now, sleep=sleep)
        request = GatedRequest(gateway, enabled=lambda: True, inner=http)
        try:
            assert await request.do_request('https://synthetic.invalid/bot0/sendMessage', method='POST',
                                            request_data=RequestData()) == (200, b'{"ok":true,"result":true}')
            assert len(calls) == 3 and len(waits) == 2 and all(delay >= 2 for delay in waits)
            assert gateway.counters.backoffs == 2
        finally:
            await request.shutdown()

    asyncio.run(run())


@pytest.mark.parametrize('failure,expected', [('connect', 'transient'), ('timeout', 'unknown'),
                                             ('rejected', 'rejected')])
def test_request_classifies_delivery_certainty(failure, expected):
    import httpx
    from telegram.error import NetworkError
    from looper.agents.channels.outbound import classify
    from looper.agents.channels.telegram_request import GatedRequest, TelegramWriteError

    async def run():
        async def http(**kwargs):
            if failure == 'rejected':
                return 400, b'{"ok":false,"description":"Bad Request"}'
            cause = httpx.ConnectError('connect failed') if failure == 'connect' else httpx.ReadTimeout('lost')
            raise NetworkError('synthetic network error') from cause

        class Once:
            async def write(self, kind, call):
                return await call()

        request = GatedRequest(Once(), enabled=lambda: True, inner=http)
        try:
            with pytest.raises(TelegramWriteError) as caught:
                await request.do_request('https://synthetic.invalid/sendMessage', method='POST')
            assert classify(caught.value)[0] == expected
        finally:
            await request.shutdown()
    asyncio.run(run())


def test_request_routes_all_writes_and_checks_switch_each_time():
    from types import SimpleNamespace
    from looper.agents.channels.telegram_request import GatedRequest

    async def run():
        enabled, routed, raw = False, [], []

        async def http(url, **kwargs):
            raw.append(url.rsplit('/', 1)[-1])
            return 200, b'{"ok":true,"result":true}'

        class Gate:
            async def write(self, kind, call):
                routed.append(kind)
                return await call()

            async def edit(self, key, call):
                routed.append(key)
                return await call()

        request = GatedRequest(Gate(), enabled=lambda: enabled, inner=http)
        data = SimpleNamespace(parameters={'chat_id': 7, 'message_id': 9})
        try:
            writes = ['sendMessage', 'editMessageText', 'setMessageReaction', 'sendDocument', 'sendPhoto',
                      'sendChatAction', 'setMyCommands', 'answerCallbackQuery']
            for enabled in [False, True]:
                for method in [*writes, 'getMe', 'getFile', 'getUpdates']:
                    await request.do_request('https://synthetic.invalid/' + method, method='POST', request_data=data)
            assert len(routed) == len(writes) and '7:9' in routed
            assert len(raw) == 2 * (len(writes) + 3)
        finally:
            await request.shutdown()
    asyncio.run(run())


def test_request_coalesces_edits_with_the_configured_floor():
    from types import SimpleNamespace
    from looper.agents.channels.telegram_request import GatedRequest

    async def run():
        now, edits = 0., []

        async def sleep(delay):
            nonlocal now
            now += delay
            await asyncio.sleep(0)

        async def http(url, request_data, **kwargs):
            edits.append((now, request_data.parameters['text']))
            return 200, b'{"ok":true,"result":true}'

        gateway = Gateway('channel-telegram', clock=lambda: now, sleep=sleep)
        request = GatedRequest(gateway, enabled=lambda: True, inner=http)

        async def edit(text):
            return await request.do_request('https://synthetic.invalid/editMessageText', method='POST',
                request_data=SimpleNamespace(parameters={'chat_id': 7, 'message_id': 9, 'text': text}))

        try:
            await edit('first')
            await asyncio.gather(edit('second'), edit('last'))
            assert edits == [(0., 'first'), (chat_settings.outbound_limits()['edit_interval_seconds'], 'last')]
        finally:
            await gateway.stop()
            await request.shutdown()
    asyncio.run(run())


@pytest.mark.skipif(not DSN, reason='disposable PostgreSQL required')
def test_chunks_fallback_once_and_replay_does_not_repeat_earlier_chunks(tmp_path, monkeypatch, telegram_http):
    async def check(pool):
        monkeypatch.setattr(chat_settings, '_outbound_enabled', True)
        bot = adapter()
        bus = MessageBus(tmp_path)
        telegram_http.mode = 'reject'
        try:
            await bot.start(bus)
            message = answer('a' * 4000 + 'b' * 10)
            assert (await bot.deliver(message)).success
            assert (await bot.deliver(message)).success
            sends = [data for method, data in telegram_http.calls if method == 'sendMessage']
            assert [(d['text'][0], bool(d.get('parse_mode'))) for d in sends] == [
                ('a', True), ('b', True), ('b', False)]
            rows = await pool.fetch('SELECT * FROM channel_outbox ORDER BY id')
            assert len(rows) == 2 and all(row['state'] == 'sent' for row in rows)
            assert [r['idempotency_key'] for r in rows] == [derived_key(bot.channel_id, message.message_id, i)
                                                           for i in range(2)]
        finally:
            await bot.stop()
            await bus.shutdown()
    run_db(check, monkeypatch)


@pytest.mark.skipif(not DSN, reason='disposable PostgreSQL required')
def test_timeout_chunk_is_uncertain_without_plain_resend(tmp_path, monkeypatch, telegram_http):
    async def check(pool):
        monkeypatch.setattr(chat_settings, '_outbound_enabled', True)
        bot, bus = adapter(), MessageBus(tmp_path)
        try:
            await bot.start(bus)
            telegram_http.mode = 'timeout'
            assert not (await bot.deliver(answer('lost answer'))).success
            assert not (await bot.deliver(answer('lost answer'))).success
            sends = [data for method, data in telegram_http.calls if method == 'sendMessage']
            assert len(sends) == 1
            row = await pool.fetchrow('SELECT * FROM channel_outbox')
            assert row['state'] == 'uncertain' and row['error_class'] == 'unknown'
        finally:
            await bot.stop()
            await bus.shutdown()
    run_db(check, monkeypatch)


@pytest.mark.skipif(not DSN, reason='disposable PostgreSQL required')
def test_public_posts_notifications_and_status_have_outbox_receipts(tmp_path, monkeypatch, telegram_http):
    async def check(pool):
        monkeypatch.setattr(chat_settings, '_outbound_enabled', True)
        monkeypatch.setitem(chat_settings._outbound_limits, 'max_in_flight', 1)
        bot, bus = adapter(), MessageBus(tmp_path)
        try:
            await bot.start(bus)
            target = NotifyTarget(TargetKind.CHAT, '7')
            posted = await bot.post(target, 'post', reply_to='4', idempotency_key='post-key')
            assert posted.success and posted.external_ids
            assert (await bot.post(target, 'post', reply_to='4', idempotency_key='post-key')) == posted
            notified = await bot.notify('7', 'notice', idempotency_key='notice-key')
            assert notified.success and notified.external_id
            assert (await bot.notify('7', 'notice', idempotency_key='notice-key')) == notified
            await bot._send_status('7', 'phase')
            rows = await pool.fetch('SELECT * FROM channel_outbox ORDER BY id')
            assert [row['source'] for row in rows] == ['post', 'notify', 'progress']
            assert len([m for m, _ in telegram_http.calls if m == 'sendMessage']) == 3
            telegram_http.mode = 'reject'
            from looper.agents.channels.base import PostOutcome
            assert (await bot.edit(target, posted.external_ids[0], 'edit')).result == PostOutcome.REJECTED
        finally:
            await bot.stop()
            await bus.shutdown()
    run_db(check, monkeypatch)


@pytest.mark.skipif(not DSN, reason='disposable PostgreSQL required')
def test_enabled_polling_bypasses_gateway_and_reaches_inbox(tmp_path, monkeypatch, telegram_http):
    from looper.agents.channels.outbound import WriteKind

    async def check(pool):
        monkeypatch.setattr(chat_settings, '_outbound_enabled', True)
        calls = []
        original = Gateway.write

        async def write(self, kind, call, **kwargs):
            calls.append(kind)
            return await original(self, kind, call, **kwargs)

        monkeypatch.setattr(Gateway, 'write', write)
        telegram_http.updates.append({'update_id': 31, 'message': {
            'message_id': 31, 'date': int(time.time()), 'text': 'hello',
            'chat': {'id': 7, 'type': 'private'},
            'from': {'id': 7, 'is_bot': False, 'first_name': 'Synthetic'}}})
        bot, bus = adapter(), MessageBus(tmp_path)
        try:
            await bot.start(bus)
            message = await asyncio.wait_for(bus.get_queue('manager').get(), 2)
            await asyncio.wait_for(bot._app.update_queue.join(), 2)
            assert message.payload['text'] == 'hello' and await bot._inbox.processed(31)
            assert calls and all(kind == WriteKind.REACT for kind in calls)
        finally:
            await bot.stop()
            await bus.shutdown()
    run_db(check, monkeypatch)


@pytest.mark.skipif(not DSN, reason='disposable PostgreSQL required')
def test_command_replies_enter_outbox_without_repeating_on_replay(tmp_path, monkeypatch, telegram_http):
    from telegram import Update

    async def check(pool):
        monkeypatch.setattr(chat_settings, '_outbound_enabled', True)
        bot, bus = adapter(), MessageBus(tmp_path)
        try:
            await bot.start(bus)
            for number, command in enumerate(['/start', '/help'], 40):
                raw = {'update_id': number, 'message': {
                    'message_id': number, 'date': int(time.time()), 'text': command,
                    'entities': [{'type': 'bot_command', 'offset': 0, 'length': len(command)}],
                    'chat': {'id': 7, 'type': 'private'},
                    'from': {'id': 7, 'is_bot': False, 'first_name': 'Synthetic'}}}
                for _ in range(2):
                    await bot._app.update_queue.put(Update.de_json(raw, bot._app.bot))
                    await asyncio.wait_for(bot._app.update_queue.join(), 2)
            assert await pool.fetchval("SELECT count(*) FROM channel_outbox WHERE state='sent'") == 2
            assert len([m for m, _ in telegram_http.calls if m == 'sendMessage']) == 2
        finally:
            await bot.stop()
            await bus.shutdown()
    run_db(check, monkeypatch)


@pytest.mark.skipif(not DSN, reason='disposable PostgreSQL required')
@pytest.mark.parametrize('mode', [None, 'reject_card', 'timeout_card'])
def test_card_delivery_uses_one_row_and_never_resends_an_unknown_card(tmp_path, monkeypatch, telegram_http, mode):
    from unittest.mock import AsyncMock
    from looper.agents import card_actions

    async def check(pool):
        monkeypatch.setattr(chat_settings, '_outbound_enabled', True)
        monkeypatch.setattr(card_actions, 'bind', AsyncMock())
        bot, bus = adapter(), MessageBus(tmp_path)
        card = card_actions.Card('card', (card_actions.Button('Confirm', 'synthetic:confirm'),))
        message = answer('intro\n\ncard')
        message.metadata['cards'] = [card.to_dict()]
        try:
            await bot.start(bus)
            telegram_http.mode = mode
            for _ in range(2):
                assert (await bot.deliver(message)).success == (mode != 'timeout_card')
            sends = [data for method, data in telegram_http.calls if method == 'sendMessage']
            assert [(d['text'], bool(d.get('reply_markup'))) for d in sends] == (
                [('intro', False), ('card', True), ('card', False)] if mode == 'reject_card'
                else [('intro', False), ('card', True)])
            rows = await pool.fetch('SELECT * FROM channel_outbox ORDER BY id')
            assert len(rows) == 2
            assert rows[1]['state'] == ('uncertain' if mode == 'timeout_card' else 'sent')
            assert rows[1]['idempotency_key'] == derived_key(bot.channel_id, f'card:{message.message_id}', 0)
            if mode != 'reject_card':
                assert json.loads(rows[1]['body'])['reply_markup']['inline_keyboard'][0][0]['callback_data'] == (
                    'synthetic:confirm')
        finally:
            await bot.stop()
            await bus.shutdown()
    run_db(check, monkeypatch)


@pytest.mark.skipif(not DSN, reason='disposable PostgreSQL required')
@pytest.mark.parametrize('change', ['disable', 'next_card_succeeds'])
def test_failed_card_keeps_its_uncertain_receipt_across_delivery_boundaries(
        tmp_path, monkeypatch, telegram_http, change):
    from telegram.error import TimedOut
    from unittest.mock import AsyncMock
    from looper.agents import card_actions

    async def check(pool):
        monkeypatch.setattr(chat_settings, '_outbound_enabled', True)
        monkeypatch.setattr(card_actions, 'bind', AsyncMock())
        bot, bus = adapter(), MessageBus(tmp_path)
        cards = [card_actions.Card('card', (card_actions.Button('Confirm', 'synthetic:confirm'),))]
        if change == 'next_card_succeeds':
            cards.append(card_actions.Card('next', (card_actions.Button('Confirm', 'another:confirm'),)))
        message = answer('\n\n'.join(card.text for card in cards))
        message.metadata['cards'] = [card.to_dict() for card in cards]
        original = telegram_http.request

        async def request(**kwargs):
            try:
                return await original(**kwargs)
            except TimedOut:
                if change == 'disable':
                    monkeypatch.setattr(chat_settings, '_outbound_enabled', False)
                telegram_http.mode = None
                raise

        telegram_http.request = request
        try:
            await bot.start(bus)
            telegram_http.mode = 'timeout_card'
            receipt = await bot.deliver(message)
            assert not receipt.success
            assert len([m for m, _ in telegram_http.calls if m == 'sendMessage']) == 1
            rows = await pool.fetch('SELECT state FROM channel_outbox')
            assert [r['state'] for r in rows] == ['uncertain']
        finally:
            await bot.stop()
            await bus.shutdown()
    run_db(check, monkeypatch)


def test_disabled_calls_match_main_script(tmp_path, monkeypatch, telegram_http):
    monkeypatch.setattr(chat_settings, '_outbound_enabled', False)

    async def run():
        bot, bus = adapter(), MessageBus(tmp_path)
        try:
            await bot.start(bus)
            telegram_http.calls.clear()
            await bot.deliver(answer('answer'))
            await bot.notify('7', 'notice', idempotency_key='ignored')
            await bot.post(NotifyTarget(TargetKind.CHAT, '7'), 'post', reply_to='4', idempotency_key='ignored')
            await bot._send_status('7', 'phase')
            await bot._edit_status('7', '4', 'edited')
            await bot._react('7', '4')
            assert telegram_http.calls == [
                ('sendMessage', {'chat_id': 7, 'text': 'answer', 'link_preview_options': {'is_disabled': True}, 'parse_mode': 'MarkdownV2'}),
                ('sendMessage', {'chat_id': 7, 'text': 'notice', 'link_preview_options': {'is_disabled': True}}),
                ('sendMessage', {'chat_id': 7, 'text': 'post', 'link_preview_options': {'is_disabled': True}, 'reply_parameters': {
                    'allow_sending_without_reply': True, 'message_id': 4}}),
                ('sendMessage', {'chat_id': 7, 'text': 'phase', 'link_preview_options': {'is_disabled': True}}),
                ('editMessageText', {'text': 'edited', 'link_preview_options': {'is_disabled': True}, 'chat_id': 7, 'message_id': 4}),
                ('setMessageReaction', {'chat_id': 7, 'message_id': 4,
                                        'reaction': [{'emoji': '👀', 'type': 'emoji'}]})]
        finally:
            await bot.stop()
            await bus.shutdown()
    asyncio.run(run())
