"""The Manager reads and confirms recording limits, without access to captures or recording switches."""

from looper.agents.llmrec import control
from looper.db import system_settings
from tests.db_support import run_db, v1_api
from tests.test_manager_schedule_tools_db import _confirm
from tests.test_ops_tasks_db import _Chat


def test_manager_reads_and_changes_recording_limits_after_confirmation(tmp_path, monkeypatch):
    async def check(pool):
        monkeypatch.setattr(control, '_control', None)
        chat = _Chat(tmp_path)
        (view,) = await chat.say('查看录制保留限制', ('looper_settings', {'action': 'view'}))
        limits = view['result']['llmrec']
        assert limits['retention_days'] == {
            'value': 7, 'source': 'built-in', 'built_in': 7, 'minimum': 1, 'maximum': 90}
        assert limits['max_total_mb'] == {
            'value': 2048, 'source': 'built-in', 'built_in': 2048, 'minimum': 256, 'maximum': 65536}
        (asked,) = await chat.say('保留90天，容量256MiB', ('looper_settings', {
            'action': 'llmrec', 'llmrec': {'retention_days': 90, 'max_total_mb': 256}}))
        assert asked['status'] == 'needs_confirmation', asked
        assert await system_settings.get_settings(['llmrec.retention_days', 'llmrec.max_total_mb']) == {}
        done = await _confirm(chat, asked)
        assert done['status'] == 'ok', done
        async with v1_api(tmp_path, pool) as call:
            code, saved = await call('llmrec_settings.get')
            assert code == 200, saved
            assert saved['result']['retention_days']['value'] == 90
            assert saved['result']['max_total_mb']['value'] == 256
        (asked,) = await chat.say('恢复内置限制', ('looper_settings', {
            'action': 'llmrec', 'llmrec': {'retention_days': None, 'max_total_mb': None}}))
        assert asked['status'] == 'needs_confirmation', asked
        done = await _confirm(chat, asked)
        assert done['status'] == 'ok', done
        (view,) = await chat.say('查看限制', ('looper_settings', {'action': 'view'}))
        assert view['result']['llmrec'] == limits

    run_db(check, monkeypatch, timeout=30)


def test_manager_refuses_invalid_limits_and_recording_controls(tmp_path, monkeypatch):
    async def check(pool):
        chat = _Chat(tmp_path)
        for values in ({}, {'retention_days': 91}, {'max_total_mb': 255}, {'max_total_mb': 65537},
                       {'retention_days': True}, {'enabled': False}, {'purge': True}):
            (reply,) = await chat.say('修改限制', ('looper_settings', {'action': 'llmrec', 'llmrec': values}))
            assert reply['status'] == 'error' and reply['error']['code'] == 'invalid', reply
        assert await system_settings.get_settings(['llmrec.enabled', 'llmrec.retention_days', 'llmrec.max_total_mb']) == {}
        names = {tool['name'] for tool in chat.ops.listing()}
        assert not any('llm_requests' in name or 'llmrec_settings' in name for name in names)

    run_db(check, monkeypatch, timeout=30)
