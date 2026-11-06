"""Prefix regression using an explicitly synthetic multi-turn tool conversation.

A real Claude Code recording remains a separate integration requirement. This
fixture exercises six calls (three parallel pairs), two user turns and one
known client compaction, without a subscription or a runtime home directory.
"""
from copy import deepcopy
import hashlib
import json
from pathlib import Path

import pytest

from looper.agents.llmrec.dialects import ChatDialect, DIALECTS
from looper.agents.llmrec.translate import thinking_signature, to_chat


FIXTURE = Path(__file__).parent / 'fixtures/gateway/claude_session.jsonl'


def compact(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':')).encode()


def without_cache(value):
    if isinstance(value, dict):
        return {key: without_cache(item) for key, item in value.items() if key != 'cache_control'}
    if isinstance(value, list):
        return [without_cache(item) for item in value]
    return value


def translated(body, dialect, session='synthetic-session'):
    return to_chat(body, DIALECTS[ChatDialect(dialect)], {'client': 'upstream'}, session_id=session).body


@pytest.mark.parametrize('dialect', ['deepseek', 'generic', 'moonshot'])
def test_append_only_history_retains_message_bytes(dialect):
    rows = [json.loads(line) for line in FIXTURE.read_text().splitlines()]
    threads = {}
    changed = 0
    append_only = 0
    for row in rows:
        assert row['provenance'].startswith('synthetic')
        body = row['body']
        system_hash = hashlib.sha256(compact(without_cache(body['system']))).hexdigest()
        thread = (row['headers']['x-claude-code-session-id'], system_hash)
        raw = translated(body, dialect, thread[0])
        chat = json.loads(raw)
        if thread in threads:
            previous, previous_chat = threads[thread]
            history = without_cache(previous['messages'])
            if without_cache(body['messages'][:len(history)]) == history:
                assert row['client_history_changed'] is False
                append_only += 1
                assert compact(chat['tools']) == compact(previous_chat['tools'])
                assert compact(chat['messages'][0]) == compact(previous_chat['messages'][0])
                prefix = b'{"model":"upstream","messages":[' + b','.join(compact(item) for item in previous_chat['messages'])
                assert raw.startswith(prefix)
            else:
                changed += 1
                assert row['client_history_changed'] is True
                print('client-side history changed: deliberate fixture compaction')
        threads[thread] = (body, chat)
    assert (append_only, changed) == (4, 1)


@pytest.mark.parametrize('dialect', ['deepseek', 'generic', 'moonshot'])
def test_historical_message_is_independent_of_position(dialect):
    message = {'role': 'assistant', 'content': [
        {'type': 'thinking', 'thinking': 'reason', 'signature': thinking_signature('reason')},
        {'type': 'text', 'text': 'answer'}, {'type': 'tool_use', 'id': 'tool', 'name': 'Bash', 'input': {'z': 1, 'a': 2}}]}
    body = {'model': 'client', 'messages': [{'role': 'user', 'content': 'question'}, message]}
    first = json.loads(translated(body, dialect))
    body['messages'].append({'role': 'user', 'content': 'next question'})
    second = json.loads(translated(body, dialect))
    assert compact(first['messages'][1]) == compact(second['messages'][1])


@pytest.mark.parametrize('dialect', ['deepseek', 'generic', 'moonshot'])
def test_moving_cache_markers_preserves_all_request_bytes(dialect):
    first = json.loads(FIXTURE.read_text().splitlines()[3])['body']
    second = without_cache(first)
    second['system'][1]['cache_control'] = {'type': 'ephemeral'}
    second['messages'][1]['content'][0]['cache_control'] = {'type': 'ephemeral'}
    assert translated(first, dialect) == translated(second, dialect)


@pytest.mark.parametrize('dialect', ['deepseek', 'generic', 'moonshot'])
def test_repeated_input_is_byte_identical_and_retains_order(dialect):
    body = json.loads(FIXTURE.read_text().splitlines()[3])['body']
    original = deepcopy(body)
    first = translated(body, dialect)
    assert first == translated(body, dialect)
    assert body == original
    assert first.startswith(b'{"model":"upstream","messages":[')
    assert b'"arguments":"{\\"z_command\\":' in first


def test_fixture_has_multi_user_turns_parallel_tools_and_thinking():
    rows = [json.loads(line) for line in FIXTURE.read_text().splitlines()]
    history = rows[-2]['body']['messages']
    assert sum(1 for m in history if m['role'] == 'user' and isinstance(m['content'], str)) >= 2
    calls = [[b for b in m['content'] if b['type'] == 'tool_use'] for m in history if m['role'] == 'assistant']
    assert sum(map(len, calls)) >= 6
    assert any(len(group) >= 2 for group in calls)
    assert sum(b['type'] == 'thinking' for m in history if m['role'] == 'assistant' for b in m['content']) >= 2
