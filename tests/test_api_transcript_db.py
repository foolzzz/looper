"""Persistent API conversation and resume contracts through a loopback provider."""
from __future__ import annotations

import json

import pytest

from looper.agents.runtimes import ApiProtocol
from looper.agents.runtimes.api.adapter import Descriptor, KEY_ENV
from looper.agents.runtimes.api.loop import (
    MessageItem, ReasoningItem, ReplayState, ToolCall, ToolCallItem,
    ToolOutcome, ToolResultItem, item_to_json,
)
from looper.agents.runtimes.api.session import ApiSession, FORBIDDEN_ENV
from looper.agents.runtimes.api.transcript import DbTranscript
from looper.db import api_sessions
from tests.db_support import DSN, run_db
from tests.fake_model_api import FakeModelAPI, ResponseScript, text_events

pytestmark = pytest.mark.skipif(not DSN, reason='LOOPER_TEST_DATABASE_URL is not set')
PROTOCOL = ApiProtocol.OPENAI_RESPONSES
NOTE = '(the previous turn did not finish)'


class NoTools:
    async def specs(self):
        return []

    async def call(self, call):
        return ToolOutcome('tools unavailable', True, False)


async def start(root, url, *, sid=None, model='test-model'):
    prompt = root / 'prompt.md'
    prompt.write_text('Keep responses concise.')
    session = ApiSession('Manager')
    command = Descriptor(PROTOCOL, model, url, None, None, str(prompt), sid).to_argv()
    await session.start(command, cwd=str(root), signature='s', env={KEY_ENV: 'test-key'})
    return session


async def turn(session, text):
    events = []

    async def emit(event):
        events.append(event)

    session.set_turn_tools(NoTools())
    return await session.run_turn(text, emit), events


@pytest.fixture(autouse=True)
def sdk_environment(monkeypatch):
    for key in FORBIDDEN_ENV:
        monkeypatch.delenv(key, raising=False)


def test_new_runtime_resumes_history_after_restart(tmp_path, monkeypatch):
    async def run(pool):
        async with FakeModelAPI([ResponseScript(events=text_events('earlier answer')),
                                 ResponseScript(events=text_events('continued'))]) as api:
            session = await start(tmp_path, api.base_url)
            try:
                result, _ = await turn(session, 'earlier user text')
            finally:
                await session.stop()
            resumed = await start(tmp_path, api.base_url, sid=result.result_event['session_id'])
            try:
                result, _ = await turn(resumed, 'next user text')
                assert result.result_event['result'] == 'continued'
                assert api.requests[-1].body['input'][0] == {'role': 'user', 'content': 'earlier user text'}
            finally:
                await resumed.stop()
    run_db(run, monkeypatch, timeout=20)


@pytest.mark.parametrize('stored', ['missing', 'empty', 'protocol', 'malformed', 'version'])
def test_unresumable_history_sends_no_request_or_events(tmp_path, monkeypatch, stored):
    async def run(pool):
        if stored != 'missing':
            protocol = 'anthropic-messages' if stored == 'protocol' else PROTOCOL.value
            await pool.execute('INSERT INTO api_sessions VALUES ($1, $2)', 'saved', protocol)
            if stored != 'empty':
                value = item_to_json(MessageItem('user', 'old'))
                if stored == 'version':
                    value['version'] = 99
                elif stored == 'malformed':
                    value['text'] = []
                await pool.execute('INSERT INTO api_session_items (session_id,seq,turn,model,item) '
                                   'VALUES ($1,1,1,$2,$3)', 'saved', 'test-model', json.dumps(value))
        async with FakeModelAPI([]) as api:
            session = await start(tmp_path, api.base_url, sid='saved')
            try:
                result, events = await turn(session, 'new')
                assert not result.session_started and not result.prompt_sent
                assert result.result_event['errors'] == ['session saved could not be resumed']
                assert not events and not api.requests
            finally:
                await session.stop()
    run_db(run, monkeypatch, timeout=20)


@pytest.mark.parametrize('fail_first', [False, True])
def test_unfinished_turn_repair_is_staged_once_before_new_user(tmp_path, monkeypatch, fail_first):
    async def run(pool):
        history = [MessageItem('user', 'old'), ToolCallItem(ToolCall('a', 'lookup', '{}')),
                   ToolResultItem('a', 'found', False, True)]
        await api_sessions.append('saved', 1, PROTOCOL.value, 'test-model', [item_to_json(i) for i in history])
        scripts = ([ResponseScript(status=401)] if fail_first else []) + [
            ResponseScript(events=text_events('done')), ResponseScript(events=text_events('again'))]
        async with FakeModelAPI(scripts) as api:
            session = await start(tmp_path, api.base_url, sid='saved')
            try:
                if fail_first:
                    failed, _ = await turn(session, 'failed new request')
                    assert failed.result_event['is_error']
                    assert len(await api_sessions.load('saved')) == len(history)
                await turn(session, 'new request')
                await turn(session, 'following request')
                sent = api.requests[-2].body['input']
                assert sent[-2:] == [{'role': 'assistant', 'content': NOTE},
                                     {'role': 'user', 'content': 'new request'}]
                stored = await DbTranscript('saved', PROTOCOL, 'test-model').items()
                assert stored.count(MessageItem('assistant', NOTE)) == 1
                assert MessageItem('user', 'failed new request') not in stored
                rows = await api_sessions.load('saved')
                assert [r['turn'] for r in rows] == [1, 1, 1, 2, 2, 2, 3, 3]
            finally:
                await session.stop()
    run_db(run, monkeypatch, timeout=20)


def test_empty_completed_answer_needs_no_repair(tmp_path, monkeypatch):
    async def run(pool):
        await api_sessions.append('saved', 1, PROTOCOL.value, 'test-model', [
            item_to_json(MessageItem('user', 'old')), item_to_json(MessageItem('assistant', ''))])
        async with FakeModelAPI([ResponseScript(events=text_events('done'))]) as api:
            session = await start(tmp_path, api.base_url, sid='saved')
            try:
                await turn(session, 'new')
                assert NOTE not in str(api.requests[0].body)
            finally:
                await session.stop()
    run_db(run, monkeypatch, timeout=20)


def test_model_change_keeps_text_and_filters_foreign_reasoning(tmp_path, monkeypatch):
    async def run(pool):
        history = [MessageItem('user', 'old text'), ReasoningItem(('summary',),
            ReplayState(PROTOCOL, 'old-model', 'r1', 'opaque-state')), MessageItem('assistant', 'old answer')]
        await api_sessions.append('saved', 1, PROTOCOL.value, 'old-model', [item_to_json(i) for i in history])
        async with FakeModelAPI([ResponseScript(events=text_events('done'))]) as api:
            session = await start(tmp_path, api.base_url, sid='saved', model='new-model')
            try:
                await turn(session, 'new')
                body = api.requests[0].body
                assert {'role': 'user', 'content': 'old text'} in body['input']
                assert {'role': 'assistant', 'content': 'old answer'} in body['input']
                assert 'opaque-state' not in str(body)
                assert (await DbTranscript('saved', PROTOCOL, 'new-model').items())[:3] == history
            finally:
                await session.stop()
    run_db(run, monkeypatch, timeout=20)


def test_loading_absent_transcript_creates_no_metadata(monkeypatch):
    async def run(pool):
        transcript = DbTranscript('absent', PROTOCOL, 'test-model')
        assert await transcript.items() == []
        assert await pool.fetchval('SELECT count(*) FROM api_sessions') == 0
    run_db(run, monkeypatch, timeout=20)


def test_completed_requests_share_one_stored_turn_number(tmp_path, monkeypatch):
    from tests.fake_model_api import call_events

    async def run(pool):
        scripts = [ResponseScript(events=call_events(name='lookup')),
                   ResponseScript(events=text_events('first answer')),
                   ResponseScript(events=text_events('second answer'))]
        async with FakeModelAPI(scripts) as api:
            session = await start(tmp_path, api.base_url)
            try:
                first, _ = await turn(session, 'first turn')
                sid = first.result_event['session_id']
                rows = await api_sessions.load(sid)
                assert len(rows) == 4 and {row['turn'] for row in rows} == {1}
                await turn(session, 'second turn')
                rows = await api_sessions.load(sid)
                assert [row['turn'] for row in rows] == [1, 1, 1, 1, 2, 2]
            finally:
                await session.stop()
    run_db(run, monkeypatch, timeout=20)
