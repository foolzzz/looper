"""Manager gateway configuration shares dashboard validation and waits for confirmation."""

from dataclasses import replace
from unittest.mock import AsyncMock
import json

import pytest

from looper.agents.llmrec import control
from looper.agents.llmrec.gateway import GatewayKey
from looper.agents.ops.settings import TOOLS
from looper.db import gateway, system_settings
from tests.db_support import run_db, v1_api
from tests.test_ops_tasks_db import _Chat


async def _seed(pool):
    await pool.execute("""INSERT INTO llm_providers(id,name,auth,vendor_type)
        VALUES ('gateway-provider','Gateway account','api_key','custom');
        INSERT INTO llm_provider_secrets VALUES ('gateway-provider','upstream-test-secret');
        INSERT INTO llm_provider_endpoints(provider_id,protocol,base_url) VALUES
        ('gateway-provider','openai-chat','https://chat.example.invalid'),
        ('gateway-provider','anthropic-messages','https://messages.example.invalid');
        INSERT INTO llm_models(id,provider_id,protocol,name) VALUES
        ('model-gateway-chat','gateway-provider','openai-chat','chat-model'),
        ('model-gateway-messages','gateway-provider','anthropic-messages','messages-model');""")


def _chat(tmp_path):
    chat = _Chat(tmp_path)
    chat.ops._platform = replace(chat.ops._platform, runtime_root=tmp_path)
    return chat


def test_manager_gateway_tool_description_separates_read_and_change():
    description = next(tool.description for tool in TOOLS if tool.name == 'looper_settings')
    assert 'includes the gateway (enabled, port, aliases; no key)' in description
    assert description.split('gateway: ', 1)[1].startswith('change')
    assert 'to read it use view' in description


def test_manager_gateway_view_has_bindings_without_keys_or_client_environment(tmp_path, monkeypatch):
    async def check(pool):
        monkeypatch.setattr(control, '_control', None)
        key = GatewayKey(tmp_path).rotate()
        await gateway.set_alias('chat', 'model-gateway-chat')
        await system_settings.put_settings({'gateway.enabled': 'true', 'gateway.port': '32123'})
        chat = _chat(tmp_path)
        (reply,) = await chat.say('查看本地网关', ('looper_settings', {'action': 'view'}))
        assert reply['status'] == 'ok', reply
        async with v1_api(tmp_path, pool) as call:
            _, dashboard = await call('gateway.get')
        assert reply['result']['gateway'] == {
            name: dashboard['result'][name] for name in ('enabled', 'port', 'aliases')}
        assert reply['result']['gateway']['aliases'][0]['model_id'] == 'model-gateway-chat'
        assert key not in json.dumps(reply) and 'upstream-test-secret' not in json.dumps(reply)
        assert not {'key', 'key_exists', 'client_environment'} & reply['result']['gateway'].keys()

    run_db(check, monkeypatch, setup=_seed, timeout=30)


@pytest.mark.parametrize('enabled', [False, True])
@pytest.mark.parametrize('include_enabled', [False, True])
def test_manager_gateway_port_confirmation_only_lists_changes(tmp_path, monkeypatch, enabled, include_enabled):
    async def check(pool):
        monkeypatch.setattr(control, '_control', None)
        await system_settings.put_settings({'gateway.enabled': str(enabled).lower(), 'gateway.port': '32123'})
        values = {'port': 32124}
        if include_enabled:
            values['enabled'] = enabled
        chat = _chat(tmp_path)
        (asked,) = await chat.say('修改网关端口', ('looper_settings', {'action': 'gateway', 'gateway': values}))
        assert asked['status'] == 'needs_confirmation', asked
        text = chat.turns[-1].blocks[-1]
        assert '- port：32123 → 32124' in text
        assert 'enabled' not in text
        assert await system_settings.get_settings(['gateway.enabled', 'gateway.port']) == {
            'gateway.enabled': str(enabled).lower(), 'gateway.port': '32123'}

    run_db(check, monkeypatch, timeout=30)


@pytest.mark.parametrize('values,before,after', [
    ({'enabled': True, 'port': 32124}, '32123', '32124'),
    ({'port': 32124}, '32123', '32124'),
    ({'enabled': False}, 'True', 'False'),
    ({'alias_set': {'alias': 'chat', 'model_id': 'model-gateway-chat'}}, '未设置', 'model-gateway-chat'),
    ({'alias_delete': 'chat'}, 'model-gateway-chat', '未设置'),
])
def test_manager_gateway_writes_wait_for_confirmation(tmp_path, monkeypatch, values, before, after):
    async def check(pool):
        controller = control.ProxyControl(tmp_path, '', enabled=False)
        controller.gateway_enabled, controller.gateway_port = True, 32123
        monkeypatch.setattr(controller, '_start', AsyncMock(return_value=False))
        monkeypatch.setattr(control, '_control', controller)
        key = GatewayKey(tmp_path).rotate()
        await system_settings.put_settings({'gateway.enabled': 'true', 'gateway.port': '32123'})
        if 'alias_delete' in values:
            await gateway.set_alias('chat', 'model-gateway-chat')
        settings_before = await system_settings.get_settings(['gateway.enabled', 'gateway.port'])
        aliases_before = await gateway.list_aliases()
        chat = _chat(tmp_path)
        (asked,) = await chat.say('修改本地网关', ('looper_settings', {'action': 'gateway', 'gateway': values}))
        assert asked['status'] == 'needs_confirmation', asked
        assert f'{before} → {after}' in chat.turns[-1].blocks[-1]
        assert await system_settings.get_settings(['gateway.enabled', 'gateway.port']) == settings_before
        assert await gateway.list_aliases() == aliases_before
        assert not (tmp_path / '.local/llmrec/gateway.json').exists()
        (done,) = await chat.say('确认', ('looper_confirm', {'confirmation_id': asked['confirmation_id']}))
        assert done['status'] == 'ok', done
        async with v1_api(tmp_path, pool) as call:
            _, dashboard = await call('gateway.get')
        saved = done['result']['gateway']
        assert saved == {name: dashboard['result'][name] for name in ('enabled', 'port', 'aliases')}
        if 'alias_set' in values:
            assert saved['aliases'][0]['model_id'] == 'model-gateway-chat'
        elif 'alias_delete' in values:
            assert saved['aliases'] == []
        else:
            assert all(saved[name] == value for name, value in values.items())
        assert GatewayKey(tmp_path).reveal() == key
        trail = await pool.fetch('SELECT status,risk,confirmation_id FROM manager_actions WHERE tool=$1',
                                 'looper_settings')
        assert any(row['status'] == 'ok' and row['risk'] == 'high'
                   and row['confirmation_id'] == asked['confirmation_id'] for row in trail)
        assert key not in json.dumps(done)

    run_db(check, monkeypatch, setup=_seed, timeout=30)


@pytest.mark.parametrize('values,operation,arguments', [
    ({'port': 0}, 'gateway.update', {'enabled': False, 'port': 0}),
    ({'port': 65536}, 'gateway.update', {'enabled': False, 'port': 65536}),
    ({'alias_set': {'alias': 'bad alias', 'model_id': 'model-gateway-chat'}},
     'gateway.alias_set', {'alias': 'bad alias', 'model_id': 'model-gateway-chat'}),
    ({'alias_set': {'alias': 'chat', 'model_id': 'model-gateway-messages'}},
     'gateway.alias_set', {'alias': 'chat', 'model_id': 'model-gateway-messages'}),
    ({'alias_set': {'alias': 'chat', 'model_id': 'missing'}},
     'gateway.alias_set', {'alias': 'chat', 'model_id': 'missing'}),
])
def test_manager_refuses_invalid_gateway_changes_with_dashboard_error(tmp_path, monkeypatch,
                                                                      values, operation, arguments):
    async def check(pool):
        monkeypatch.setattr(control, '_control', control.ProxyControl(tmp_path, '', enabled=False))
        chat = _chat(tmp_path)
        (reply,) = await chat.say('修改网关', ('looper_settings', {'action': 'gateway', 'gateway': values}))
        assert reply['status'] == 'error', reply
        async with v1_api(tmp_path, pool) as call:
            status, dashboard = await call(operation, arguments)
        assert status == 400
        assert reply['error']['message'] == dashboard['error']['message']
        assert await system_settings.get_settings(['gateway.enabled', 'gateway.port']) == {}
        assert await gateway.list_aliases() == []
        assert await pool.fetchval("SELECT count(*) FROM manager_actions WHERE status='needs_confirmation'") == 0

    run_db(check, monkeypatch, setup=_seed, timeout=30)


@pytest.mark.parametrize('arguments', [
    {'action': 'key_reveal'}, {'action': 'key_rotate'},
    {'action': 'gateway', 'gateway': {'key_reveal': True}},
    {'action': 'gateway', 'gateway': {'key_rotate': True}},
    {'action': 'gateway'},
    {'action': 'gateway', 'gateway': {}},
    {'action': 'gateway', 'gateway': {'enabled': True, 'alias_delete': 'chat'}},
    {'action': 'gateway', 'gateway': {'alias_set': {'alias': 'chat'}}},
])
def test_manager_gateway_has_no_key_actions_or_ambiguous_writes(tmp_path, monkeypatch, arguments):
    async def check(pool):
        key = GatewayKey(tmp_path).rotate()
        chat = _chat(tmp_path)
        (reply,) = await chat.say('修改网关', ('looper_settings', arguments))
        assert reply['status'] == 'error', reply
        if arguments['action'] == 'gateway' and not arguments.get('gateway'):
            assert reply['error']['code'] == 'invalid'
            message = reply['error']['message']
            assert 'view' in message
            assert all(field in message for field in ('enabled', 'port', 'alias_set', 'alias_delete'))
        assert GatewayKey(tmp_path).reveal() == key
        assert not (tmp_path / '.local/llmrec/gateway.json').exists()
        assert await system_settings.get_settings(['gateway.enabled', 'gateway.port']) == {}
        assert key not in json.dumps(reply)
        tool = next(item for item in chat.ops.listing() if item['name'] == 'looper_settings')
        assert 'dashboard-only' in tool['description']
        assert not any('key' in name for name in tool['inputSchema']['properties']['gateway']['properties'])

    run_db(check, monkeypatch, timeout=30)


def test_manager_gateway_rechecks_target_after_confirmation(tmp_path, monkeypatch):
    async def check(pool):
        chat = _chat(tmp_path)
        (asked,) = await chat.say('设置别名', ('looper_settings', {'action': 'gateway', 'gateway': {
            'alias_set': {'alias': 'chat', 'model_id': 'model-gateway-chat'}}}))
        assert asked['status'] == 'needs_confirmation', asked
        await pool.execute("UPDATE llm_providers SET enabled=false WHERE id='gateway-provider'")
        (reply,) = await chat.say('确认', ('looper_confirm', {'confirmation_id': asked['confirmation_id']}))
        assert reply['status'] == 'error' and reply['error']['code'] == 'invalid', reply
        assert await gateway.list_aliases() == []

    run_db(check, monkeypatch, setup=_seed, timeout=30)
