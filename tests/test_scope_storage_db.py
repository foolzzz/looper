"""Read scope authority and keep retired session state durable across late saves."""

import pytest

from looper.agents.chat_scopes import Override, PrincipalKind, ScopeConfig
from looper.db import chat_bindings, manager_sessions
from tests.db_support import DSN, run_db
from tests.test_manager_sessions_db import _row

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")


def test_current_sets_use_current_version_and_skip_deleted_and_missing(monkeypatch):
    async def check(pool):
        from looper.db import capability_sets

        await pool.execute("INSERT INTO capability_set_versions(set_id,version,items) "
                           "VALUES ('chat_only',2,'[{\"kind\":\"chat\"},{\"kind\":\"vault.read\",\"paths\":[]}]')")
        await pool.execute("UPDATE capability_sets SET current_version=2 WHERE id='chat_only'")
        await pool.execute("UPDATE capability_sets SET deleted_at=now() WHERE id='legacy'")
        rows = await capability_sets.current(['chat_only', 'full', 'legacy', 'set-missing'], pool=pool)
        assert set(rows) == {'chat_only', 'full'}
        assert rows['chat_only'] == (2, [{'kind': 'chat'}, {'kind': 'vault.read', 'paths': []}])
        assert rows['full'][0] == 1 and {'kind': 'shell'} in rows['full'][1]
        assert await capability_sets.current([], pool=pool) == {}

    run_db(check, monkeypatch, timeout=30)


def test_scope_config_reads_owner_default_and_all_sender_aliases(monkeypatch):
    async def check(pool):
        await pool.execute("INSERT INTO managers(id,name,system_prompt,tools) "
                           "VALUES ('manager-alpha','Alpha','Help','all')")
        await pool.execute("INSERT INTO channel_configs(channel_id,channel_type) VALUES ('channel-lark','lark')")
        await pool.execute("INSERT INTO manager_chat_bindings(channel_id,chat_id,manager_id,default_scope,legacy_allowed) "
                           "VALUES ('channel-lark','oc_synthetic','manager-alpha','legacy',true)")
        for sender in ('sender-a', 'sender-b'):
            await pool.execute("INSERT INTO binding_principal_scopes "
                               "(channel_id,chat_id,sender_id,principal_id,principal_kind,scope_id) "
                               "VALUES ('channel-lark','oc_synthetic',$1,'principal-bot','bot','chat_only')", sender)
        expected = Override('principal-bot', PrincipalKind.BOT, 'chat_only')
        assert await chat_bindings.scope_config('channel-lark', 'oc_synthetic', 'manager-alpha', pool=pool) == (
            ScopeConfig(True, 'legacy', True, {'sender-a': expected, 'sender-b': expected}))
        for chat, manager in [('oc_synthetic', 'manager-other'), ('oc_missing', 'manager-alpha')]:
            assert await chat_bindings.scope_config('channel-lark', chat, manager, pool=pool) == (
                ScopeConfig(False, None, False, {}))

    run_db(check, monkeypatch, timeout=30)


def test_late_save_keeps_retirement_and_reopen_preserves_totals(monkeypatch):
    async def check(pool):
        await pool.execute("INSERT INTO managers(id,name,system_prompt,tools) "
                           "VALUES ('manager-other','Other','Help','all')")
        fingerprint = '0123456789abcdef'
        row = _row('oc_synthetic', pending_handoff='old handoff', clean_start=True,
                   scope_id='chat_only', scope_version=1, audience='shared', scope_fingerprint=fingerprint)
        row['session_key'] += '~' + fingerprint
        plain = _row('oc_synthetic')
        other_chat = _row('oc_other')
        other_manager = plain | {'manager_id': 'manager-other'}
        for item in (row, plain, other_chat, other_manager):
            await manager_sessions.save(item, pool=pool)
        active = await manager_sessions.active_for_chat('manager', 'channel-lark-1', 'oc_synthetic', pool=pool)
        assert set(active) == {(row['session_key'], fingerprint), (plain['session_key'], None)}
        await pool.execute("UPDATE manager_sessions SET activity_cursor=123 WHERE manager_id='manager' AND session_key=$1",
                           row['session_key'])
        await manager_sessions.retire('manager', [row['session_key'], plain['session_key']], 'scope_changed', pool=pool)
        retired = await manager_sessions.get('manager', row['session_key'], pool=pool)
        await manager_sessions.save(row | {'session_id': 'finished-old-turn'}, pool=pool)
        late = await manager_sessions.get('manager', row['session_key'], pool=pool)
        assert late['retired_at'] == retired['retired_at'] is not None
        assert late['retired_reason'] == 'scope_changed' and late['activity_cursor'] == 123
        assert (late['scope_id'], late['scope_version'], late['audience'], late['scope_fingerprint']) == (
            'chat_only', 1, 'shared', fingerprint)
        assert await manager_sessions.active_for_chat('manager', 'channel-lark-1', 'oc_synthetic', pool=pool) == []
        assert len(await manager_sessions.active_for_chat('manager-other', 'channel-lark-1', 'oc_synthetic', pool=pool)) == 1
        await manager_sessions.retire('manager', [row['session_key']], 'other_reason', pool=pool)
        assert (await manager_sessions.get('manager', row['session_key'], pool=pool))['retired_reason'] == 'scope_changed'
        await manager_sessions.reopen('manager', row['session_key'], pool=pool)
        reopened = await manager_sessions.get('manager', row['session_key'], pool=pool)
        assert all(reopened[name] is None for name in (
            'session_id', 'pending_handoff', 'retired_at', 'retired_reason', 'activity_cursor'))
        assert reopened['clean_start'] is False and reopened['session_totals'] == row['session_totals']
        assert reopened['scope_fingerprint'] == fingerprint
        assert (await manager_sessions.get('manager', plain['session_key'], pool=pool))['retired_at'] is not None

    run_db(check, monkeypatch, timeout=30)
