"""Memory proposals through the bound Manager tool: sources, refusal and atomic commit."""
import asyncio
import json
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest

from looper.agents.memory_model import fingerprint
from looper.agents.ops import Call, ErrorCode, OpError, TurnOrigin
from looper.agents.ops import memory_propose as tool
from looper.db import memory as db_memory
from tests.db_support import DSN, run_db, run_with_channel_configs
from tests import memory_support as mem
from tests import memory_propose_support as sup

pytestmark = pytest.mark.skipif(not DSN, reason='LOOPER_TEST_DATABASE_URL is not set')


def test_operator_turn_proposes_a_drafting_candidate_whose_source_is_the_call(monkeypatch, tmp_path):
    async def check(pool):
        await sup.setup(pool, tmp_path)
        current = sup.turn()
        answer = await sup.propose(sup.ops(), current)
        assert answer['status'] == 'ok', answer
        assert answer['result']['state'] == 'drafting'
        row = await pool.fetchrow('SELECT * FROM memory_candidates')
        assert (row['source_kind'], row['source_class'], row['manager_id']) == ('manager_turn', 'operator', 'manager')
        action = await pool.fetchrow('SELECT * FROM manager_actions')
        source = await pool.fetchrow('SELECT * FROM memory_candidate_sources')
        assert source['manager_action_id'] == action['id'] and source['quote'] == sup.STATEMENT
        assert action['status'] == 'ok'
        assert json.loads(action['result']) == {'candidate': answer['result']['candidate'], 'state': 'drafting'}
        event = await pool.fetchrow('SELECT * FROM memory_candidate_events')
        assert (event['action'], event['actor_kind'], event['via']) == ('propose', 'manager', 'dashboard')
        assert current.blocks == [f'（已提议记入记忆 {answer["result"]["candidate"]}：{sup.STATEMENT}，待你确认）']
    run_db(check, monkeypatch)


def test_operator_direct_chat_proposes_with_via_lark(monkeypatch, tmp_path):
    async def check(pool):
        await sup.setup(pool, tmp_path)
        await pool.execute("UPDATE channel_configs SET config=$1 WHERE channel_id='channel-lark'",
                           json.dumps({'operator_sender_id': 'ou_synthetic_operator'}))
        current = sup.turn()
        current.channel, current.channel_id = 'lark', 'channel-lark'
        current.sender = current.chat = 'ou_synthetic_operator'
        assert (await sup.propose(sup.ops(), current))['status'] == 'ok'
        assert await pool.fetchval('SELECT via FROM memory_candidate_events') == 'lark'
    run_with_channel_configs(check, monkeypatch)


@pytest.mark.parametrize('role,session,age', [('assistant','memory-chat',0), ('user','other-chat',0),
                                            ('user','memory-chat',25), ('user','memory-chat',0)])
def test_quote_the_user_did_not_say_here_is_invalid(monkeypatch, tmp_path, role, session, age):
    async def check(pool):
        await sup.setup(pool, tmp_path)
        # A positive control proves the same-session, recent-user lookup works.
        await pool.execute('INSERT INTO chat_messages(session_id,message_id,role,content,created_at) VALUES($1,\'stored-negative\',$2,$3,$4)',
                           session, role, sup.STATEMENT if age or role != 'user' or session != 'memory-chat' else 'other',
                           datetime.now(timezone.utc)-timedelta(hours=age))
        answer = await sup.propose(sup.ops(), sup.turn('hello'))
        assert answer['error'] == {'code':'invalid', 'message':"quote must be the user's own words in this conversation"}
        assert not await pool.fetchval('SELECT count(*) FROM memory_candidates')
        assert await pool.fetchval('SELECT status FROM manager_actions') == 'error'
        await pool.execute('INSERT INTO chat_messages(session_id,message_id,role,content) VALUES($1,\'stored-positive\',\'user\',$2)',
                           'memory-chat', sup.STATEMENT)
        assert (await sup.propose(sup.ops(), sup.turn('hello', 'm-2')))['status'] == 'ok'
    run_db(check, monkeypatch)


@pytest.mark.parametrize('fields', [{'group':True, 'channel':'lark', 'channel_id':'channel-lark'}, {'client_id':'synthetic-client'},
    {'channel':'lark','sender':'ou_other','channel_id':'channel-lark'}])
def test_other_turns_are_refused_in_the_tools_own_words(monkeypatch, tmp_path, fields):
    async def check(pool):
        await sup.setup(pool, tmp_path)
        current = sup.turn()
        for key, value in fields.items():
            setattr(current, key, value)
        service = sup.ops()
        assert tool.TOOL_NAME not in [t['name'] for t in await service.current_listing(current)]
        answer = await sup.propose(service, current)
        assert answer['error'] == {'code':'forbidden','message':tool.REFUSAL}
    run_with_channel_configs(check, monkeypatch)


@pytest.mark.parametrize('origin', [TurnOrigin.OWN, TurnOrigin.COMPLETION])
def test_claudes_own_turn_needs_a_user_turn(monkeypatch, tmp_path, origin):
    async def check(pool):
        await sup.setup(pool, tmp_path)
        answer = await sup.propose(sup.ops(), sup.turn(origin=origin))
        assert answer['error']['code'] == 'needs_user_turn'
    run_db(check, monkeypatch)


@pytest.mark.parametrize('field', ['statement','why','quote'])
def test_credentials_are_refused_and_nothing_is_stored(monkeypatch, tmp_path, field):
    async def check(pool):
        await sup.setup(pool, tmp_path)
        answer = await sup.propose(sup.ops(), sup.turn(), **{field:'sk-syntheticToken1234567890'})
        assert answer['error']['code'] == 'invalid' and '凭据' in answer['error']['message']
        assert not await pool.fetchval('SELECT count(*) FROM memory_candidates')
    run_db(check, monkeypatch)


@pytest.mark.parametrize('keywords', [[], ['x']*7, ['  ']])
def test_invalid_keywords_are_refused(monkeypatch, tmp_path, keywords):
    async def check(pool):
        await sup.setup(pool, tmp_path)
        result = await sup.propose(sup.ops(), sup.turn(), keywords=keywords)
        assert result['error']['code'] == 'invalid' and 'keywords' in result['error']['message']
    run_db(check, monkeypatch)


def test_turn_and_day_limits(monkeypatch, tmp_path):
    async def check(pool):
        await sup.setup(pool, tmp_path)
        service, current = sup.ops(), sup.turn()
        for _ in range(2):
            assert (await sup.propose(service, current))['status'] == 'ok'
            current.state = current.state.ANSWERING
        # Hold both preflights at the boundary so the transaction must arbitrate the last slot.
        preflight = tool._check_caps
        ready, arrived = asyncio.Event(), 0

        async def together(conn, call, now):
            nonlocal arrived
            await preflight(conn, call, now)
            if not conn.is_in_transaction():
                arrived += 1
                if arrived == 2:
                    ready.set()
                await asyncio.wait_for(ready.wait(), 5)

        monkeypatch.setattr(tool, '_check_caps', together)
        answers = await asyncio.gather(*(sup.propose(sup.ops(), sup.turn()) for _ in range(2)))
        assert sorted(a['status'] for a in answers) == ['error', 'ok']
        assert next(a for a in answers if a['status']=='error')['error']['code'] == 'rate_limited'
        assert await pool.fetchval('SELECT count(*) FROM memory_candidates') == 3
        for index in range(17):
            assert (await sup.propose(sup.ops(), sup.turn(message_id=f'day-{index}')))['status'] == 'ok'
        result = await sup.propose(sup.ops(), sup.turn(message_id='last'))
        assert result['error']['code'] == 'rate_limited'
    run_db(check, monkeypatch)


def test_revise_reseeds_a_pending_proposal_of_this_manager(monkeypatch, tmp_path):
    async def check(pool):
        await sup.setup(pool, tmp_path)
        answer = await sup.propose(sup.ops(), sup.turn())
        candidate = answer['result']['candidate']
        ident = await pool.fetchval('SELECT id FROM memory_candidates')
        await mem.revise(pool, ident)
        current = sup.turn('以后周报只写结论。', 'm-2')
        result = await sup.propose(sup.ops(), current, statement='以后周报只写结论。', quote='以后周报只写结论。', revise=candidate)
        assert result['status'] == 'ok', result
        row = await pool.fetchrow('SELECT * FROM memory_candidates')
        assert row['state'] == 'drafting' and row['expires_at'] is None
        assert row['fingerprint'] == fingerprint('preference', '以后周报只写结论。')
        assert await pool.fetchval('SELECT count(*) FROM memory_candidate_sources') == 2
        assert await pool.fetchval('SELECT count(*) FROM memory_candidate_revisions') == 1
        assert current.blocks[-1].startswith('（已修改记忆提议 ')
        assert await pool.fetchval('SELECT action FROM memory_candidate_events ORDER BY id DESC LIMIT 1') == 'propose'
    run_db(check, monkeypatch)


@pytest.mark.parametrize('change,code', [('drafting','conflict'), ('confirmed','conflict'), ('written','conflict'),
    ('other_manager','not_found'), ('untrusted','invalid'), ('other_kind','invalid'), ('missing','not_found')])
def test_revise_refusals(monkeypatch, tmp_path, change, code):
    async def check(pool):
        await sup.setup(pool, tmp_path)
        result = await sup.propose(sup.ops(), sup.turn())
        ident = await pool.fetchval('SELECT id FROM memory_candidates')
        if change in ('confirmed','written'):
            await mem.confirmed(pool, ident)
            if change == 'written':
                await mem.move(pool, ident, 'writing', 'claim')
                await mem.move(pool, ident, 'written', 'write', commit_sha='a'*40)
        elif change != 'drafting':
            await mem.revise(pool, ident)
        if change == 'other_manager':
            await pool.execute("UPDATE memory_candidates SET manager_id=NULL WHERE id=$1", ident)
        if change == 'untrusted':
            await pool.execute('UPDATE memory_candidates SET untrusted=true WHERE id=$1', ident)
        if change == 'other_kind':
            raw = json.loads(await pool.fetchval("SELECT value FROM system_settings WHERE key='memory_write'"))
            raw['targets']['project'] = raw['targets']['preference']
            await pool.execute("UPDATE system_settings SET value=$1 WHERE key='memory_write'", json.dumps(raw))
        answer = await sup.propose(sup.ops(), sup.turn(message_id='m-2'),
            revise='M-9999' if change == 'missing' else result['result']['candidate'],
            **({'kind':'project'} if change == 'other_kind' else {}))
        assert answer['error']['code'] == code, answer
    run_db(check, monkeypatch)


def test_operator_reproposal_clears_a_tombstone(monkeypatch, tmp_path):
    async def check(pool):
        await sup.setup(pool, tmp_path)
        await pool.execute("INSERT INTO memory_tombstones(fingerprint,kind,destination,deleted_via) VALUES($1,'preference','vault','dashboard')",
                           fingerprint('preference', sup.STATEMENT))
        assert (await sup.propose(sup.ops(), sup.turn()))['status'] == 'ok'
        assert not await pool.fetchval('SELECT count(*) FROM memory_tombstones')
    run_db(check, monkeypatch)


def test_switch_turned_off_after_listing_refuses_without_writing(monkeypatch, tmp_path):
    async def check(pool):
        await sup.setup(pool, tmp_path)
        await pool.execute("UPDATE system_settings SET value='{}' WHERE key='memory_write'")
        with pytest.raises(OpError) as error:
            await tool.TOOL.run(Call(sup.ARGS, sup.turn(), None))
        assert error.value.code is ErrorCode.CONFLICT
        assert not await pool.fetchval('SELECT count(*) FROM memory_candidates')
        assert (await sup.propose(sup.ops(), sup.turn()))['error']['message'].endswith(tool.TOOL_NAME)
    run_db(check, monkeypatch)


@pytest.mark.parametrize('failure', ['create','record'])
def test_failed_commit_proposes_nothing(monkeypatch, tmp_path, failure):
    async def check(pool):
        await sup.setup(pool, tmp_path)
        service, current = sup.ops(), sup.turn()
        if failure == 'create':
            monkeypatch.setattr(db_memory, 'create', AsyncMock(side_effect=RuntimeError('synthetic failure')))
        else:
            monkeypatch.setattr(service, '_record', AsyncMock(return_value=None))
        answer = await sup.propose(service, current)
        assert answer['error']['code'] == 'unavailable'
        assert not await pool.fetchval('SELECT count(*) FROM memory_candidates')
        assert current.blocks == []
        if failure == 'create':
            assert await pool.fetchval('SELECT status FROM manager_actions') == 'error'
    run_db(check, monkeypatch)


@pytest.mark.parametrize('written', [False, True])
def test_deleting_the_managers_actions_removes_unwritten_proposals(monkeypatch, tmp_path, written):
    async def check(pool):
        await sup.setup(pool, tmp_path)
        await sup.propose(sup.ops(), sup.turn())
        ident = await pool.fetchval('SELECT id FROM memory_candidates')
        if written:
            await mem.confirmed(pool, ident)
            await mem.move(pool, ident, 'writing','claim')
            await mem.move(pool, ident, 'written','write', commit_sha='a'*40)
        await pool.execute('DELETE FROM manager_actions')
        row = await pool.fetchrow('SELECT * FROM memory_candidates')
        assert (row['purged_at'] is not None and row['statement'] == '') if written else row is None
    run_db(check, monkeypatch)
