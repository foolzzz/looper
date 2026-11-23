"""Assistant eval contracts and their disposable-server positive/negative scripts."""
from datetime import datetime, timezone
from dataclasses import replace
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from evals import assistant_experience as e
from evals.prompt_regression import assistant_metrics
from tests.db_support import DSN


def window(**kwargs):
    return e.Window('', datetime.now(timezone.utc), 'abcdefgh', **kwargs)


def test_memory_requires_search_evidence_and_preserves_the_source():
    w = window()
    w.reply = 'abcdefgh，来自 operator.md'
    w.actions = [{'tool': 'looper_vault_search', 'risk': 'read', 'status': 'ok',
                  'arguments': {'query': 'operator-fact'}}]
    w.vault_searches = {'operator-fact': [{'path': 'operator.md', 'snippet': 'abcdefgh'}]}
    w.vault_unchanged = True
    assert e.memory_recalled(w) is None
    w.actions[0]['status'] = 'error'
    assert e.memory_recalled(w)
    w.actions[0]['status'] = 'ok'
    w.vault_unchanged = False
    assert e.memory_recalled(w)


def test_absence_requires_empty_search_and_a_positive_control():
    w = window()
    w.reply = '没有记录'
    w.actions = [{'tool': 'looper_vault_search', 'risk': 'read', 'status': 'ok',
                  'arguments': {'query': query}}
                 for query, results in [('control', [{'path': 'operator.md'}]), ('missing-fact', [])]]
    w.vault_searches = {'control': [{'path': 'operator.md'}], 'missing-fact': []}
    assert e.memory_absent(w) is None
    w.actions.pop(0)
    assert e.memory_absent(w)
    w.reply = '你的偏好是 abcdefgh'
    assert e.memory_absent(w)


@pytest.mark.parametrize('field,limit', [('ack_ms', 2000), ('first_reply_ms', 15000)])
def test_latency_thresholds_and_invalid_or_unmeasured_turns(field, limit):
    check = e.receipt_latency if field == 'ack_ms' else e.first_reply_latency
    metric = {'v': 1, 'origin': 'user', 'chat': 'direct', 'replayed': False,
              'received_at': datetime.now(timezone.utc).isoformat(), 'ack': 'reaction',
              'ack_ms': 2000, 'first_reply_ms': 15000, 'delivery': 'reply', 'tool_calls': 0}
    w = window(turn_metrics=[metric])
    w.reply = '2'
    assert check(w) is None
    for value in (limit + 1, None, -1, True, float('nan')):
        metric[field] = value
        assert check(w), value
    metric[field] = limit
    for key, value in [('replayed', True), ('origin', 'completion'), ('delivery', 'empty')]:
        original = metric[key]
        metric[key] = value
        assert check(w), (key, value)
        metric[key] = original
    metric['tool_calls'] = 1
    assert check(w)  # no corresponding top-level action in the database
    assert check(window())


def test_first_reply_does_not_depend_on_a_successful_reaction():
    w = window(turn_metrics=[{
        'v': 1, 'origin': 'user', 'chat': 'direct', 'replayed': False,
        'received_at': datetime.now(timezone.utc).isoformat(), 'ack': 'pending',
        'ack_ms': None, 'first_reply_ms': 100, 'delivery': 'reply', 'tool_calls': 0}])
    w.reply = '2'
    assert e.first_reply_latency(w) is None
    assert e.receipt_latency(w)


def test_proactivity_checks_repetition_database_receipts_and_quiet():
    w = window(replies_by_turn=['提醒：abcdefgh', '明白。'])
    w.actions = [{'tool': 'looper_reminder_list', 'risk': 'read', 'status': 'ok'}]
    w.reminders = [{'id': 1, 'chat': e.Chat.DM, 'text': 'abcdefgh', 'status': 'delivered', 'attempts': 1, 'due': True}]
    w.proactive_rows = [{'source': 'user_reminder', 'source_ref': '1', 'outcome': 'sent',
                         'channel_id': 'channel-lark', 'chat_kind': 'direct', 'chat': e.Chat.DM, 'sent': True}]
    assert e.reminder_once(w) is None
    w.reminders[0]['due'] = False
    assert e.reminder_once(w)
    w.reminders[0]['due'] = True
    w.replies_by_turn.append('再次提醒：abcdefgh')
    assert e.reminder_once(w)
    w.replies_by_turn.pop()
    w.reminders[0]['status'] = 'pending'
    assert e.reminder_once(w)
    w.reminders = []
    w.proactive_rows = []
    w.replies_by_turn = ['早上好。', '明白。']
    assert e.nothing_to_push(w) is None
    w.proactive_rows.append({'source': 'autonomous', 'outcome': 'sent'})
    assert e.nothing_to_push(w)


@pytest.mark.parametrize('session_routing', [False, True])
def test_scripted_reminder_receipt_uses_the_inserted_id(tmp_path, monkeypatch, session_routing):
    import asyncpg

    reminders = [{'id': 1, 'text': 'pre-existing reminder'}]
    deliveries = []

    async def insert(sql, *args):
        if sql.startswith('INSERT INTO reminders'):
            reminders.append({'id': reminders[-1]['id'] + 1, 'text': args[0]})
            return reminders[-1]['id']
        if sql.startswith('INSERT INTO proactive_deliveries'):
            deliveries.append({'source_ref': args[1]})
        return 0

    async def fetch(sql, *args):
        if 'FROM reminders' in sql:
            return reminders
        if 'FROM proactive_deliveries' in sql:
            return deliveries
        return []

    conn = SimpleNamespace(execute=AsyncMock(side_effect=insert), fetchval=AsyncMock(side_effect=insert),
                           fetch=AsyncMock(side_effect=fetch), close=AsyncMock())
    monkeypatch.setattr(asyncpg, 'connect', AsyncMock(return_value=conn))
    monkeypatch.setattr(e.runner, 'select_agent', Mock())
    # No dialogue is needed to exercise source seeding and receipt creation.
    case = replace(next(c for c in e.CASES if c.id == 'proactive_reminder'), says=(), checks=())
    args = SimpleNamespace(fake=True, model='sonnet', manager_model=None, timeout=1,
                           session_routing=session_routing)
    session = e.Session(args, case, SimpleNamespace(OPEN_ID='ou_eval_operator'), False, None)
    api = Mock()
    api.post.return_value = {'result': {'enabled': False}}
    result = session.run(api, 'unused', tmp_path / 'world.json', tmp_path / 'calls.jsonl', {}, tmp_path)

    assert reminders == [{'id': 1, 'text': 'pre-existing reminder'}, {'id': 2, 'text': session.secret}]
    assert result['proactive_rows'] == [{'source_ref': '2'}]
    routing_calls = [call for call in api.post.call_args_list
                     if 'session_routing_enabled' in call.args[1]]
    if session_routing:
        assert len(routing_calls) == 1
        assert routing_calls[0].args == (
            '/api/v1/session_routing/update', {'session_routing_enabled': True})
    else:
        assert routing_calls == []


def test_needs_you_must_have_a_failing_source_and_not_repeat_in_another_post():
    w = window(replies_by_turn=['需要你处理：abcdefgh', '明白。'])
    w.actions = [{'tool': 'looper_schedules', 'risk': 'read', 'status': 'ok'}]
    w.schedules = [{'id': 'asst-needs-you', 'name': 'abcdefgh', 'consecutive_failures': 1}]
    w.proactive_rows = [{'source': 'autonomous', 'source_ref': 'asst-needs-you', 'outcome': 'sent',
                         'channel_id': 'channel-lark', 'chat_kind': 'direct', 'chat': e.Chat.DM, 'sent': True}]
    assert e.needs_you_once(w) is None
    w.visible_posts = [*w.replies_by_turn, 'abcdefgh']
    assert e.needs_you_once(w)
    w.visible_posts = []
    w.proactive_rows[0]['chat'] = e.Chat.OTHER_DM
    assert e.needs_you_once(w)
    w.proactive_rows[0]['chat'] = e.Chat.DM
    w.schedules[0]['consecutive_failures'] = 0
    assert e.needs_you_once(w)


def test_online_definitions_read_manager_metrics_without_filling_missing_data():
    raw = {'enabled': True, 'window_days': 7,
           'ack': {'p50_ms': 100, 'p95_ms': 200, 'by_chat': {}},
           'first_reply': {'p50_ms': 500, 'p95_ms': 900, 'by_chat': {}},
           'empty': {'count': 1, 'ratio': .25},
           'proactive': {'per_day': [], 'reply_rate': {'all': None, 'by_source': {}}}}
    result = assistant_metrics.online_metrics(raw)
    assert set(result['values']) == {'receipt_latency', 'first_reply_latency', 'empty_delivery', 'proactivity'}
    assert result['values']['receipt_latency'] == raw['ack']
    assert result['values']['proactivity']['reply_rate']['all'] is None
    assert assistant_metrics.online_metrics({'enabled': False})['values'] is None
    with pytest.raises(ValueError, match='missing'):
        assistant_metrics.online_metrics({'enabled': True})


def test_documented_counts_match_the_collected_conversations():
    from evals.prompt_regression.dialogs import DIALOGS
    readme = (Path(__file__).resolve().parents[1] / 'evals/README.md').read_text()
    assert len(e.CASES) == 27
    assert sum(len(case.says) for case in e.CASES) == 37
    assert 'Twenty-seven conversations' in readme
    assert f'all {len(DIALOGS)} dialogs' in readme
    assert {c.category.value for c in e.CASES} == {
        'time', 'identity', 'crosstalk', 'tone', 'memory', 'proactivity', 'latency'}


@pytest.mark.skipif(not DSN, reason='LOOPER_TEST_DATABASE_URL is not set')
@pytest.mark.parametrize('case_id', [
    'memory_vault', 'memory_absent', 'proactive_reminder', 'proactive_needs_you',
    'proactive_quiet', 'latency_receipt', 'latency_first_reply',
])
def test_new_conversation_positive_and_negative_on_disposable_server(tmp_path, monkeypatch, case_id):
    monkeypatch.setenv('CLAUDE_CONFIG_DIR', str(tmp_path / 'claude'))
    spec = importlib.util.spec_from_file_location('lark_cli_stand_in', e.lark.HERE / 'tests/lark_cli_stand_in.py')
    stand_in = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(stand_in)
    args = SimpleNamespace(fake=True, model='sonnet', manager_model=None, manager_effort=None,
                           claude=None, timeout=60, pg_url=DSN, port=0,
                           ack_max_ms=2000, first_reply_max_ms=15000,
                           session_routing=case_id == 'memory_vault')
    case = next(c for c in e.CASES if c.id == case_id)
    for failing in (False, True):
        result = e.lark.run_once(args, stand_in, tmp_path / str(failing), 'direct',
                                session=e.Session(args, case, stand_in, failing, None))
        assert result['checks']['reply_received'] is None, result
        assert result['passed'] is not failing, result
        for check in case.checks:
            assert (result['checks'][check.__name__] is None) is not failing, result
        assert result['online_metrics']['values'] is not None
        values = result['online_metrics']['values']
        assert result['online_metrics']['turns']['by_delivery'] == {'reply': len(case.says)}
        assert values['empty_delivery'] == {'count': 0, 'ratio': 0}
        assert values['proactivity']['reply_rate']['all'] is None
        assert sum(sum(day['by_source'].values()) for day in values['proactivity']['per_day']) == len(result['proactive_rows'])
        if case.category == e.Category.LATENCY:
            assert all(metric['ack'] == 'reaction' for metric in result['turn_metrics'])
            assert values['receipt_latency']['p50_ms'] == 2000 + int(failing)
            assert values['first_reply_latency']['p95_ms'] == 15000 + int(failing)
        assert result['fake_observations'] == (case.category in (e.Category.PROACTIVITY, e.Category.LATENCY))
        json.dumps(result)  # exported evidence remains JSON-safe
