"""Scope storage contracts on fresh, upgraded and partially migrated databases."""

import asyncio
import json

import asyncpg
import pytest

from looper.db import chat_bindings, connection
from tests.db_support import DSN, fresh_connection

pytestmark = pytest.mark.skipif(not DSN, reason="requires disposable LOOPER_TEST_DATABASE_URL")

_ROLLBACK = """DROP TABLE binding_principal_scopes, capability_set_versions, capability_sets CASCADE;
ALTER TABLE manager_chat_bindings DROP COLUMN default_scope, DROP COLUMN legacy_allowed, DROP COLUMN suggested_scope;
ALTER TABLE manager_sessions DROP COLUMN scope_id, DROP COLUMN scope_version, DROP COLUMN audience,
    DROP COLUMN scope_fingerprint, DROP COLUMN retired_at, DROP COLUMN retired_reason, DROP COLUMN activity_cursor,
    DROP CONSTRAINT IF EXISTS manager_sessions_key,
    ADD CONSTRAINT manager_sessions_key CHECK (session_key = kind || ':' || channel_id || ':' || chat);
ALTER TABLE chat_messages DROP COLUMN manager_session, DROP COLUMN sender_kind, DROP COLUMN sender_name;
ALTER TABLE managers DROP CONSTRAINT managers_session_routing_check,
    ADD CONSTRAINT managers_session_routing_check CHECK (session_routing IN ('off', 'per_chat'));"""
_SEED = """
INSERT INTO managers(id,name,system_prompt,tools) VALUES
    ('manager-alpha','Alpha','Help','all'), ('manager-beta','Beta','Help','tasks');
INSERT INTO channel_configs(channel_id,channel_type) VALUES ('channel-lark','lark');
INSERT INTO manager_chat_bindings(channel_id,chat_id,manager_id) VALUES
    ('channel-lark','oc_alpha','manager-alpha'), ('channel-lark','oc_beta','manager-beta');
"""
_OVERRIDE = """INSERT INTO binding_principal_scopes
    (channel_id,chat_id,sender_id,principal_id,principal_kind,scope_id)
    VALUES ('channel-lark','oc_alpha','sender-test','principal-test','person','chat_only')"""


@pytest.mark.parametrize("repeat", [False, True])
def test_upgrade_marks_existing_bindings_legacy(repeat):
    async def check():
        async with fresh_connection() as conn:
            await connection.ensure_schema(conn)
            await conn.execute(_ROLLBACK)
            await conn.execute(_SEED)
            await conn.execute("UPDATE managers SET session_routing='per_chat' WHERE id='manager-alpha'")
            await conn.execute("INSERT INTO manager_sessions(manager_id,session_key,kind,channel_id,chat) "
                "VALUES ('manager-alpha','group:channel-lark:oc_alpha','group','channel-lark','oc_alpha')")
            await connection.ensure_schema(conn)
            if repeat:
                await connection.ensure_schema(conn)
            rows = await conn.fetch("SELECT default_scope,legacy_allowed,suggested_scope "
                                    "FROM manager_chat_bindings ORDER BY chat_id")
            assert [tuple(r) for r in rows] == [('legacy', True, 'chat_only'), ('legacy', True, None)]
            row = await conn.fetchrow("SELECT scope_id,scope_version,audience,scope_fingerprint,retired_at,"
                                     "retired_reason,activity_cursor FROM manager_sessions")
            assert tuple(row) == (None,) * 7
            assert [tuple(r) for r in await conn.fetch("SELECT id,session_routing FROM managers "
                "WHERE id IN ('manager-alpha','manager-beta') ORDER BY id")] == [
                    ('manager-alpha', 'per_chat'), ('manager-beta', 'off')]
            assert not await connection.schema_change_pending(conn)
    asyncio.run(check())


def test_backfill_runs_once():
    async def check():
        async with fresh_connection() as conn:
            await connection.ensure_schema(conn)
            await conn.execute(_ROLLBACK)
            await conn.execute(_SEED)
            await connection.ensure_schema(conn)
            await conn.execute("INSERT INTO manager_chat_bindings(channel_id,chat_id,manager_id) "
                               "VALUES ('channel-lark','oc_new','manager-alpha')")
            # Force apply with its marker still present, after the one-time backfill.
            await conn.execute("ALTER TABLE chat_messages DROP COLUMN sender_name")
            await connection.ensure_schema(conn)
            await connection.ensure_schema(conn)
            row = await conn.fetchrow("SELECT default_scope,legacy_allowed,suggested_scope "
                                     "FROM manager_chat_bindings WHERE chat_id='oc_new'")
            assert tuple(row) == (None, False, None)
            assert await conn.fetchval("SELECT default_scope FROM manager_chat_bindings WHERE chat_id='oc_alpha'") == 'legacy'
    asyncio.run(check())


def test_builtins_seeded():
    async def check():
        async with fresh_connection() as conn:
            await connection.ensure_schema(conn)
            rows = await conn.fetch("SELECT id,builtin,current_version,version,items FROM capability_sets "
                "JOIN capability_set_versions ON set_id=id ORDER BY id")
            full = [{"kind": "chat"}, {"kind": "tasks", "projects": "manager",
                "actions": ["read", "create", "instruct", "start", "stop", "restart"],
                "visibility": "project", "templates_only": False},
                {"kind": "schedules", "actions": ["read", "manage"]}, {"kind": "shell"}]
            assert [(r['id'],r['builtin'],r['current_version'],r['version'],json.loads(r['items']))
                    for r in rows] == [('chat_only', True, 1, 1, [{"kind": "chat"}]),
                                      ('full', True, 1, 1, full), ('legacy', True, 1, 1, full)]
    asyncio.run(check())


def test_jsonb_path_exists_is_immutable():
    async def check():
        async with fresh_connection() as conn:
            await connection.ensure_schema(conn)
            assert await conn.fetchval("SELECT provolatile FROM pg_proc WHERE "
                "oid='jsonb_path_exists(jsonb,jsonpath,jsonb,boolean)'::regprocedure") == b'i'
    asyncio.run(check())


_REJECT = [
    ('chat-only-shell', "INSERT INTO capability_set_versions VALUES ('chat_only',2,'[{\"kind\":\"shell\"}]',now())"),
    ('full-no-shell', "INSERT INTO capability_set_versions VALUES ('full',2,'[{\"kind\":\"chat\"}]',now())"),
    ('custom-shell', "INSERT INTO capability_sets VALUES ('set-qa','QA',false,1,NULL); "
                     "INSERT INTO capability_set_versions VALUES ('set-qa',1,'[{\"kind\":\"shell\"}]',now())"),
    ('invalid-set-id', "INSERT INTO capability_sets VALUES ('qa','QA',false,1,NULL)"),
    *[(f'binding-{label}', f"UPDATE manager_chat_bindings SET {assignment} WHERE chat_id='oc_alpha'")
      for label, assignment in [('full', "default_scope='full'"), ('legacy', "default_scope='legacy',legacy_allowed=false"),
                                ('suggested-legacy', "suggested_scope='legacy'"), ('suggested-full', "suggested_scope='full'")]],
    *[(f'override-{label}', _OVERRIDE.replace("'chat_only'", f"'{label}'")) for label in ('full','legacy')],
    ('override-kind', _OVERRIDE.replace("'person'", "'robot'")),
    ('override-unbound', _OVERRIDE.replace("'oc_alpha'", "'oc_unbound'")),
    *[(f'session-{label}', "INSERT INTO manager_sessions(manager_id,session_key,kind,channel_id,chat,scope_id,audience,scope_fingerprint) "
       f"VALUES ('manager-alpha','group:channel-lark:oc_alpha','group','channel-lark','oc_alpha',{scope},{audience},{fingerprint})")
      for label,scope,audience,fingerprint in [
          ('full-null', "'full'", 'NULL', 'NULL'), ('full-shared', "'full'", "'shared'", 'NULL'),
          ('chat-operator', "'chat_only'", "'operator'", 'NULL'), ('null-shared', 'NULL', "'shared'", 'NULL'),
          ('audience', "'legacy'", "'everyone'", 'NULL'), ('key', 'NULL', 'NULL', "'fingerprint'")]],
    ('sender-kind', "INSERT INTO chat_messages(session_id,message_id,role,sender_kind) VALUES ('test','msg','user','robot')"),
    ('routing', "UPDATE managers SET session_routing='other' WHERE id='manager-alpha'"),
]


@pytest.mark.parametrize("label,statement", _REJECT, ids=[case[0] for case in _REJECT])
def test_check_rejects(label, statement):
    async def check():
        async with fresh_connection() as conn:
            await connection.ensure_schema(conn)
            await conn.execute(_SEED)
            assert await conn.fetchval("SELECT convalidated FROM pg_constraint WHERE "
                "conrelid='chat_messages'::regclass AND conname='chat_messages_sender_kind_check'")
            with pytest.raises((asyncpg.CheckViolationError, asyncpg.ForeignKeyViolationError)):
                await conn.execute(statement)
    asyncio.run(check())


def test_check_accepts():
    async def check():
        async with fresh_connection() as conn:
            await connection.ensure_schema(conn)
            await conn.execute(_SEED)
            for index, (scope,audience,fingerprint) in enumerate([
                ('full','operator',None), (None,None,None), ('legacy','shared',None), ('chat_only','shared','fingerprint')]):
                chat = f'oc_test{index}'
                key = f'group:channel-lark:{chat}' + (f'~{fingerprint}' if fingerprint else '')
                await conn.execute("INSERT INTO manager_sessions(manager_id,session_key,kind,channel_id,chat,scope_id,audience,scope_fingerprint) "
                    "VALUES ('manager-alpha',$1,'group','channel-lark',$2,$3,$4,$5)", key,chat,scope,audience,fingerprint)
            for index, kind in enumerate([None,'person','bot']):
                await conn.execute("INSERT INTO chat_messages(session_id,message_id,role,sender_kind) VALUES ('test',$1,'user',$2)", str(index),kind)
            await conn.execute("UPDATE managers SET session_routing='scoped' WHERE id='manager-alpha'")
            await conn.execute(_OVERRIDE)
            assert await conn.fetchval("SELECT count(*) FROM binding_principal_scopes") == 1
            await conn.execute("DELETE FROM manager_chat_bindings WHERE chat_id='oc_alpha'")
            assert await conn.fetchval("SELECT count(*) FROM binding_principal_scopes") == 0
            assert await conn.fetchval("SELECT count(*) FROM manager_sessions") == 4
    asyncio.run(check())


def test_rebinding_resets_scope():
    async def check():
        async with fresh_connection() as conn:
            await connection.ensure_schema(conn)
            await conn.execute(_SEED)
            await conn.execute("UPDATE manager_chat_bindings SET default_scope='legacy',legacy_allowed=true,"
                               "suggested_scope='chat_only' WHERE chat_id='oc_alpha'")
            await conn.execute(_OVERRIDE)
            created_at = await conn.fetchval("SELECT created_at FROM manager_chat_bindings WHERE chat_id='oc_alpha'")
            for owner, expected in [('manager-alpha', ('legacy',True,'chat_only')), ('manager-beta', (None,False,None))]:
                async with conn.transaction():
                    binding = await chat_bindings.put_binding('channel-lark','oc_alpha',owner,conn=conn)
                row = await conn.fetchrow("SELECT default_scope,legacy_allowed,suggested_scope FROM manager_chat_bindings WHERE chat_id='oc_alpha'")
                assert tuple(row) == expected
                assert binding['created_at'] == created_at
                assert set(binding) == {'channel_id','chat_id','manager_id','created_at','allowed_senders','sender_policy',
                                        'default_scope','effective_default_scope','legacy_allowed','suggested_scope'}  # the operations member's scope/risk fields
                assert await conn.fetchval("SELECT count(*) FROM binding_principal_scopes") == (owner == 'manager-alpha')
            async with conn.transaction():
                await chat_bindings.delete_binding('channel-lark','oc_alpha',conn=conn)
                await chat_bindings.put_binding('channel-lark','oc_alpha','manager-alpha',conn=conn)
            assert not await conn.fetchval("SELECT legacy_allowed FROM manager_chat_bindings WHERE chat_id='oc_alpha'")
    asyncio.run(check())


@pytest.mark.parametrize('damage', [
    'DROP TABLE binding_principal_scopes',
    'ALTER TABLE manager_sessions DROP COLUMN scope_version',
    "ALTER TABLE manager_sessions DROP CONSTRAINT manager_sessions_key, "
    "ADD CONSTRAINT manager_sessions_key CHECK (session_key = kind || ':' || channel_id || ':' || chat)",
    'ALTER TABLE chat_messages DROP CONSTRAINT chat_messages_sender_kind_check',
    "ALTER TABLE managers DROP CONSTRAINT managers_session_routing_check, "
    "ADD CONSTRAINT managers_session_routing_check CHECK (session_routing IN ('off','per_chat'))",
])
def test_partial_upgrade_repairs_with_marker_present(damage):
    async def check():
        async with fresh_connection() as conn:
            await connection.ensure_schema(conn)
            await conn.execute(_SEED)
            await conn.execute("INSERT INTO schema_migrations(name) VALUES ('chat_scopes'),('chat_scopes_online')")
            await conn.execute(damage)
            assert await connection.schema_change_pending(conn)
            await connection.ensure_schema(conn)
            assert not await connection.schema_change_pending(conn)
            row = await conn.fetchrow("SELECT default_scope,legacy_allowed FROM manager_chat_bindings WHERE chat_id='oc_alpha'")
            assert tuple(row) == (None, False)
    asyncio.run(check())


def test_upgrade_validates_online_after_commit():
    async def check():
        async with fresh_connection() as conn:
            await connection.ensure_schema(conn)
            await conn.execute(_ROLLBACK)
            await conn.execute(_SEED)
            # A stale online marker cannot hide newly rebuilt NOT VALID checks.
            await conn.execute("INSERT INTO schema_migrations(name) VALUES ('chat_scopes'),('chat_scopes_online')")
            async with conn.transaction():
                await connection.ensure_schema(conn)
                checks = await conn.fetch("SELECT oid,convalidated FROM pg_constraint WHERE "
                    "(conrelid='chat_messages'::regclass AND conname='chat_messages_sender_kind_check') OR "
                    "(conrelid='managers'::regclass AND conname='managers_session_routing_check') ORDER BY oid")
                assert len(checks) == 2 and all(not r['convalidated'] for r in checks)
                assert not await conn.fetchval("SELECT EXISTS (SELECT 1 FROM schema_migrations WHERE name='chat_scopes_online')")
            await connection.ensure_schema(conn)
            assert await conn.fetchval("SELECT bool_and(convalidated) FROM pg_constraint WHERE oid=ANY($1::oid[])",
                                       [r['oid'] for r in checks])
            assert await conn.fetchval("SELECT EXISTS (SELECT 1 FROM schema_migrations WHERE name='chat_scopes_online')")
    asyncio.run(check())
