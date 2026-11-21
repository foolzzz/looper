"""Operator settings validation, cache refresh and default-off side effects."""
import asyncio
from dataclasses import asdict
from unittest.mock import AsyncMock

import pytest

from looper.agents import api_v1, chat_settings
from tests.test_api_v1_auth import CLIENT_TOKEN, _bearer, _send, _serving, clients as clients


@pytest.mark.parametrize('body', [
    {'timezone': 'Missing/Zone'}, {'timezone': ''}, {'timezone': 8},
    {'reply_language': 'fr'}, {'turn_context_enabled': 1},
])
def test_bad_context_setting_is_atomic(monkeypatch, body):
    put = AsyncMock()
    monkeypatch.setattr(chat_settings.db_settings, 'put_settings', put)
    with pytest.raises(ValueError):
        asyncio.run(chat_settings.update(body))
    put.assert_not_awaited()


def test_settings_store_reload_reset_and_off_has_no_channel_reads(monkeypatch):
    from looper.agents.channels import operator_ids
    state = {}
    writes = []
    async def put(values):
        writes.append(values)
        for key, value in values.items():
            if value is None:
                state.pop(key, None)
            else:
                state[key] = value
    async def get(keys):
        return {key: state[key] for key in keys if key in state}
    monkeypatch.setattr(chat_settings.db_settings, 'put_settings', put)
    monkeypatch.setattr(chat_settings.db_settings, 'get_settings', get)
    missing = AsyncMock(return_value=[operator_ids.BotRef('bot-example', 'lark', 'Example bot')])
    monkeypatch.setattr(operator_ids, 'missing', missing)
    monkeypatch.setattr(chat_settings, '_turn_context_enabled', False)
    monkeypatch.setattr(chat_settings, '_timezone', None)
    monkeypatch.setattr(chat_settings, '_reply_language', 'zh-CN')
    async def run():
        await chat_settings.load()
        assert not chat_settings.turn_context_enabled()
        await chat_settings.update({'stop_enabled': False})
        assert writes == [{'chat.stop_enabled': 'false'}]
        missing.assert_not_awaited()
        result = await chat_settings.update({'turn_context_enabled': True, 'timezone': 'Asia/Shanghai', 'reply_language': 'en'})
        assert result['missing_operator_ids'] == [asdict(missing.return_value[0])]
        assert chat_settings.timezone() == 'Asia/Shanghai' and chat_settings.reply_language() == 'en'
        await chat_settings.update({'turn_context_enabled': None, 'timezone': None, 'reply_language': None})
        assert not chat_settings.turn_context_enabled() and chat_settings.timezone() is None
        assert chat_settings.reply_language() == 'zh-CN'
        assert state == {'chat.stop_enabled': 'false'}
    asyncio.run(run())


@pytest.mark.parametrize('action', ['get', 'update'])
def test_context_settings_are_operator_only(tmp_path, monkeypatch, clients, action):
    async def run():
        async with _serving(tmp_path) as server:
            status, _, answer = await _send(server, f'POST /api/v1/chat_settings/{action}', _bearer(CLIENT_TOKEN), '{}')
            assert status == 403 and answer['error']['code'] == 'forbidden'
    asyncio.run(run())
    operation = api_v1.find('POST', f'/api/v1/chat_settings/{action}')
    assert operation.operator_only and operation.tool is None
