"""Operator settings validation, cache refresh and default-off side effects."""
import asyncio
import json
from types import SimpleNamespace
from dataclasses import asdict
from unittest.mock import AsyncMock

import pytest

from looper.agents import api_v1, chat_settings
from looper.agents.ops.clients import ClientOps
from tests.test_api_v1_auth import CLIENT_TOKEN, _bearer, _send, _serving, clients as clients


@pytest.mark.parametrize('body', [
    {'timezone': 'Missing/Zone'}, {'timezone': ''}, {'timezone': 8},
    {'reply_language': 'fr'}, {'turn_context_enabled': 1}, {},
    {'server_timezone': 'UTC'}, {'stop_enabled': True},
    {'turn_context_enabled': True, 'timezone': 'Missing/Zone'},
])
def test_bad_context_setting_is_atomic(monkeypatch, body):
    put = AsyncMock()
    monkeypatch.setattr(chat_settings.db_settings, 'put_settings', put)
    with pytest.raises(ValueError):
        asyncio.run(chat_settings.update_turn_context(body))
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
    monkeypatch.setattr(chat_settings.db_ledger, 'enabled', AsyncMock(return_value=False))
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
        result = await chat_settings.update_turn_context({'turn_context_enabled': True, 'timezone': 'Asia/Shanghai', 'reply_language': 'en'})
        assert result['missing_operator_ids'] == [asdict(missing.return_value[0])]
        assert chat_settings.timezone() == 'Asia/Shanghai' and chat_settings.reply_language() == 'en'
        await chat_settings.update_turn_context({'turn_context_enabled': None, 'timezone': None, 'reply_language': None})
        assert not chat_settings.turn_context_enabled() and chat_settings.timezone() is None
        assert chat_settings.reply_language() == 'zh-CN'
        assert state == {'chat.stop_enabled': 'false'}
    asyncio.run(run())


@pytest.mark.parametrize('action,body', [
    ('get', {}), ('update', {'turn_context_enabled': True}),
    ('update', {'timezone': 'Asia/Shanghai'}), ('update', {'reply_language': 'en'}),
])
def test_new_context_settings_are_operator_only(tmp_path, monkeypatch, clients, action, body):
    operation = api_v1.find('POST', f'/api/v1/turn_context_settings/{action}')
    assert operation is not None and operation.operator_only and operation.tool is None
    run = AsyncMock(side_effect=AssertionError('client reached operator settings'))
    monkeypatch.setattr(api_v1.domain_for(operation), 'run', run)
    async def check():
        async with _serving(tmp_path) as server:
            server._client_ops = ClientOps(SimpleNamespace())
            status, _, answer = await _send(server, f'POST {operation.path}', _bearer(CLIENT_TOKEN), json.dumps(body))
            assert status == 403 and answer['error']['code'] == 'forbidden'
            run.assert_not_awaited()
    asyncio.run(check())


@pytest.mark.parametrize('field,value', [
    ('turn_context_enabled', True), ('timezone', 'UTC'), ('reply_language', 'en'), ('server_timezone', 'UTC'),
])
def test_context_fields_are_not_chat_settings(monkeypatch, field, value):
    put = AsyncMock()
    monkeypatch.setattr(chat_settings.db_settings, 'put_settings', put)
    with pytest.raises(ValueError, match='unknown setting'):
        asyncio.run(chat_settings.update({field: value, 'stop_enabled': True}))
    put.assert_not_awaited()
    operation = api_v1.find('POST', '/api/v1/chat_settings/update')
    assert field not in operation.request['properties']
    assert field not in api_v1.find('POST', '/api/v1/chat_settings/get').result['properties']


def test_context_update_declares_confirmation_and_validates_before_impact():
    operation = api_v1.find('POST', '/api/v1/turn_context_settings/update')
    assert operation is not None and operation.risk == 'high' and operation.confirms
    from looper.agents.ops import OpError
    async def check():
        domain = api_v1.domain_for(operation)
        impact = await domain.impact(None, operation, {'timezone': 'UTC'}, api_v1.OPERATOR, {})
        assert 'user.timezone' in impact
        with pytest.raises(OpError) as refused:
            await domain.impact(None, operation, {'timezone': 'Missing/Zone'}, api_v1.OPERATOR, {})
        assert refused.value.code.value == 'invalid'
    asyncio.run(check())


def test_clients_keep_read_access_to_chat_settings(tmp_path, monkeypatch, clients):
    monkeypatch.setattr(chat_settings, 'settings_view', AsyncMock(return_value={'stop_enabled': False}))
    async def run():
        async with _serving(tmp_path) as server:
            status, _, answer = await _send(server, 'POST /api/v1/chat_settings/get', _bearer(CLIENT_TOKEN), '{}')
            assert status == 200 and answer['result']['stop_enabled'] is False
    asyncio.run(run())
    assert not api_v1.find('POST', '/api/v1/chat_settings/get').operator_only


@pytest.mark.parametrize('body,expected', [
    ({'turn_context_enabled': False}, {'manager.turn_context.enabled': 'false'}),
    ({'timezone': 'UTC'}, {'user.timezone': 'UTC'}),
    ({'reply_language': 'en'}, {'user.reply_language': 'en'}),
])
def test_context_writes_only_named_fields(monkeypatch, body, expected):
    put = AsyncMock()
    monkeypatch.setattr(chat_settings.db_settings, 'put_settings', put)
    monkeypatch.setattr(chat_settings.db_settings, 'get_settings', AsyncMock(return_value={}))
    asyncio.run(chat_settings.update_turn_context(body))
    put.assert_awaited_once_with(expected)


def test_bot_lookup_failure_does_not_enable_context(monkeypatch):
    from looper.agents.channels import operator_ids
    put = AsyncMock()
    monkeypatch.setattr(chat_settings.db_settings, 'put_settings', put)
    monkeypatch.setattr(operator_ids, 'missing', AsyncMock(side_effect=RuntimeError('unavailable')))
    with pytest.raises(RuntimeError, match='unavailable'):
        asyncio.run(chat_settings.update_turn_context({'turn_context_enabled': True}))
    put.assert_not_awaited()


@pytest.mark.parametrize("metrics,context", [(True, False), (False, True), (True, True)])
def test_metrics_and_turn_context_reload_independently(monkeypatch, metrics, context):
    stored = {
        chat_settings.METRICS_ENABLED_KEY: str(metrics).lower(),
        chat_settings.TURN_CONTEXT_KEY: str(context).lower(),
        chat_settings.TIMEZONE_KEY: "Asia/Shanghai",
        chat_settings.REPLY_LANGUAGE_KEY: "en",
    }

    async def get(keys):
        return {key: stored[key] for key in keys if key in stored}

    monkeypatch.setattr(chat_settings.db_settings, "get_settings", get)
    monkeypatch.setattr(chat_settings, "_metrics_enabled", not metrics)
    monkeypatch.setattr(chat_settings, "_turn_context_enabled", not context)
    monkeypatch.setattr(chat_settings, "_timezone", None)
    monkeypatch.setattr(chat_settings, "_reply_language", "zh-CN")
    asyncio.run(chat_settings.load())
    assert chat_settings.metrics_enabled() is metrics
    assert chat_settings.turn_context_enabled() is context
    assert chat_settings.timezone() == "Asia/Shanghai"
    assert chat_settings.reply_language() == "en"
