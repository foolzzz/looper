"""Delivery records preserve workflow evidence and truncate only whole entries."""
from __future__ import annotations

import json

from looper.agents.tiers.journal import JournalEntry, JournalKind
from looper.agents.tiers.planner import PlanStep, TaskPlan
from looper.agents.user_gates import Gate, GateState, GateSummary
from looper.agents.workflow_reporting import process_record, process_record_text

SAFETY = 'Remote-environment safety (read-only / dry run) is not enforced by Looper'


def entry(kind, step='s1', **data):
    return JournalEntry(step, step, json.dumps(data), kind=kind)


def task(*steps):
    return TaskPlan('plan', 'goal', 'Deliver the change', '', steps=list(steps))


def test_roles_keep_configured_profiles_requested_and_reported_models_and_fallbacks():
    runs = [{'id': 1, 'role': 'worker', 'model': 'reported-model', 'metadata': {
        'profile': {'name': 'Backup', 'runtime': {'type': 'example-runtime'}},
        'model_choice': {'model': 'requested-model'},
        'prompt_layers': {'task_role': {'key': 'dev'}}, 'fallback_from': 'primary'}}]
    record = process_record_text(process_record(task(), [entry(JournalKind.PROFILE_FALLBACK, role='dev',
                                          **{'from': 'primary', 'to': 'backup', 'reason': 'profile_error'})], runs,
                            workflow={'roles': [{'key': 'dev', 'profile_id': 'primary'},
                                                {'key': 'review_a', 'profile_id': 'review-profile'}]}))
    for text in ('dev', 'primary', 'review_a', 'review-profile', 'Backup', 'example-runtime',
                 'requested-model', 'reported-model', '回落', 'profile_error'):
        assert text in record


def test_review_rounds_keep_each_seat_adjudications_late_results_and_stale_seats():
    reviews = {'s1/review_a': {'status': 'verified', 'result': {'passed': False, 'findings': [
        {'severity': 'blocking', 'where': 'app.py:1', 'text': 'Check boundary'}]}},
        's1/review_b': {'status': 'dispatched', 'result': None}}
    entries = [entry(JournalKind.REVIEW_ROUND, round=1, reviews=reviews),
               entry(JournalKind.REVIEW_ROUND, round=1, stale=['s1/review_b'],
                     reviews={'s1/review_b': None}),
               entry(JournalKind.REVIEW_ADJUDICATED, member='review_a', finding=0, level='suggestion',
                     reason='The caller validates input'),
               entry(JournalKind.LATE_REVIEW, 's1/review_b', passed=True, findings=[])]
    record = process_record_text(process_record(task(), entries, []))
    for text in ('第 2 轮', 's1/review_a', '1 blocking', '失败', 's1/review_b', 'stale', '裁决',
                 'The caller validates input', '晚到', '通过'):
        assert text in record


def test_verification_keeps_each_head_result_and_unenforced_remote_safety():
    record = process_record_text(process_record(task(), [entry(JournalKind.VERIFICATION_ROUND, attempt=1, head='abc123',
                                          status='failed', failure='verification_changed_code')], []))
    assert '第 1 轮' in record and 'abc123' in record and 'failed' in record
    assert 'verification_changed_code' in record and SAFETY in record


def test_all_gates_include_topics_and_conclusions_including_pending():
    plan = task(PlanStep('g1', 0, 'Continue?', key='g1', gate=Gate('Continue?', (), state=GateState.PENDING)),
                PlanStep('g2', 1, 'Next action', key='g2', gate=Gate('Next action', (),
                    state=GateState.PASSED, summary=GateSummary(('Proceed with repair',), ('Use default',), ()))))
    record = process_record_text(process_record(plan, [entry(JournalKind.ESCALATION, gates=['g1'], reason='Attempts exhausted')], []))
    assert 'Continue?' in record and 'pending' in record
    assert 'Next action' in record and 'Proceed with repair' in record and 'Use default' in record
    assert 'Attempts exhausted' in record


def test_legacy_workflow_reviews_are_recorded_without_panel_journal():
    plan = task(PlanStep('s1', 0, 'Build', key='s1', result={'verify_passed': True, 'role_reviews': [
        {'role': 'review_a', 'passed': True, 'findings': []}]}))
    record = process_record_text(process_record(plan, [], []))
    assert 'review_a' in record and 'builtin' in record and '通过' in record


def test_record_truncates_whole_entries_and_counts_omissions():
    entries = [entry(JournalKind.VERIFICATION_ROUND, f'v{i}', attempt=i + 1,
                     head=f'HEAD-{i}-' + 'a' * 1300, status='completed') for i in range(100)]
    data = process_record(task(), entries, [])
    assert len(json.dumps(data, ensure_ascii=False)) <= 60000
    record = process_record_text(data, max_chars=60000)
    assert SAFETY in record
    kept = sum(f'HEAD-{i}-' in record for i in range(100))
    assert data['omitted'] == 100 - kept > 0
    assert f'已省略 {100 - kept} 条记录' in record
    for i in range(100):
        if f'HEAD-{i}-' in record:
            assert f'HEAD-{i}-' + 'a' * 1300 in record


def test_manager_text_keeps_evidence_beyond_summary_limit_and_marks_truncation():
    data = process_record(task(), [entry(JournalKind.VERIFICATION_ROUND, 'v1',
        attempt=1, head='abc123', status='failed', summary='word ' * 1500)], [])
    text = process_record_text(data)
    assert 1000 < len(text) <= 6000
    assert text.endswith('…truncated')
    assert 'abc123' in text and 'failed' in text
    assert len(data['sections']['Verification'][0]) > 6000


def test_missing_reported_model_is_not_substituted_with_requested_model():
    record = process_record_text(process_record(task(), [], [{'id': 1, 'role': 'worker', 'model': None, 'metadata': {
        'model_choice': {'model': 'requested-model'}}}]))
    assert '请求模型 requested-model; 报告模型 未记录' in record


def test_text_section_order_survives_jsonb_key_reordering():
    record = process_record(task(), [], [])
    record['sections'] = dict(reversed(list(record['sections'].items())))
    text = process_record_text(record)
    assert [text.index('### ' + name) for name in ('Roles', 'Reviews', 'Verification', 'Escalations')] == sorted(
        text.index('### ' + name) for name in ('Roles', 'Reviews', 'Verification', 'Escalations'))
