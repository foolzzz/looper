"""Delivery records preserve workflow evidence and truncate only whole entries."""
from __future__ import annotations

import json

import pytest

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
    record = process_record_text(data, max_chars=300000)
    assert SAFETY in record
    kept = sum(f'HEAD-{i}-' in record for i in range(100))
    assert data['omitted'] == 100 - kept > 0
    assert f'已省略 {100 - kept} 条记录' in record
    for i in range(100):
        if f'HEAD-{i}-' in record:
            assert f'HEAD-{i}-' + 'a' * 1300 in record


def test_manager_text_omits_oversized_entry_and_keeps_safety():
    data = process_record(task(), [entry(JournalKind.VERIFICATION_ROUND, 'v1',
        attempt=1, head='abc123', status='failed', summary='word ' * 1500)], [])
    text = process_record_text(data)
    assert len(text) <= 6000
    assert '已省略 1 条记录' in text
    assert 'abc123' not in text and SAFETY in text
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


@pytest.mark.parametrize("runtime,metadata,expected", [
    ("cursor-agent", {"model_display": "actual-cursor"}, "actual-cursor"),
    ("cursor-agent", {"model_display": "display", "model_reported": "actual"}, "actual"),
    ("cursor-agent", {"model_display": "display", "model_reported": None}, "未记录"),
    ("cursor-agent", {}, "未记录"),
    *[(runtime, {}, "未记录") for runtime in ("codex", "grok", "pi", "omp", "dsh")],
    ("claude-code", {}, "legacy-model"),
    ("codex", {"model_reported": "actual"}, "actual"),
])
def test_reported_models_follow_runs_api_rules(runtime, metadata, expected):
    record = process_record_text(process_record(task(), [], [{"id": 1, "role": "worker", "model": "legacy-model",
        "metadata": {"profile": {"runtime": {"type": runtime}}, "model_choice": {"model": "auto"}, **metadata}}]))
    assert f"请求模型 auto; 报告模型 {expected}" in record


def test_many_runs_keep_all_sections_whole_entries_safety_and_omissions():
    runs = [{"id": i, "role": "worker", "model": "actual", "metadata": {
        "profile": {"name": "Dev", "runtime": {"type": "claude-code"}},
        "model_choice": {"model": "requested"}}} for i in range(72)]
    journal = [entry(JournalKind.REVIEW_ROUND, round=0, reviews={"review_a": {"passed": True}}),
               entry(JournalKind.VERIFICATION_ROUND, attempt=1, head="checked-head", status="completed"),
               entry(JournalKind.ESCALATION, reason="Operator decided", gates=["g1"])]
    journal += [entry(JournalKind.REVIEW_ADJUDICATED, reason="oversized " * 5000)]
    data = process_record(task(), journal, runs)
    text = process_record_text(data)
    assert len(text) <= 6000
    assert "72 worker runs on actual" in text
    assert all(piece in text for piece in ("review_a", "checked-head", "Operator decided", SAFETY))
    assert "已省略 1 条记录" in text
    assert "oversized" not in text
    for name, items in data["sections"].items():
        for item in items:
            assert item in text or "oversized" in item


def test_delivery_summary_bounds_each_section_and_keeps_the_pointer():
    from looper.agents.workflow_reporting import process_record_summary

    data = process_record(task(), [entry(JournalKind.REVIEW_ROUND, round=2,
        reviews={"review_a": {"passed": False, "findings": ["Blocking"]}}),
        entry(JournalKind.VERIFICATION_ROUND, attempt=2, head="checked-head", status="failed"),
        entry(JournalKind.ESCALATION, reason="Needs approval", gates=["g1"])], [
            {"id": i, "role": "worker", "model": "actual", "metadata": {
                "profile": {"name": "Dev", "runtime": {"type": "claude-code"}},
                "model_choice": {"model": "requested"}, "fallback_from": "primary"}}
            for i in range(72)])
    text = process_record_summary(data)
    assert len(text) <= 1200
    assert len(text.splitlines()) == 6
    assert all(piece in text for piece in ("worker", "claude-code", "actual", "primary", "第 3 轮", "失败",
                                           "第 2 轮", "failed", "Needs approval"))
    assert text.endswith("Full record: task page / looper_tasks get")



def test_delivery_summary_uses_final_rounds_even_when_details_are_oversized():
    from looper.agents.workflow_reporting import process_record_summary

    journal = []
    for attempt in range(1, 13):
        journal.extend((entry(JournalKind.REVIEW_ROUND, round=attempt - 1,
            reviews={"review_a": {"passed": attempt == 12, "findings": []}}),
            entry(JournalKind.VERIFICATION_ROUND, attempt=attempt,
                  status="completed" if attempt == 12 else "failed", summary="details " * 1500)))
    runs = [{"id": 1, "role": "worker", "model": "primary-model", "metadata": {
        "profile": {"name": "Profile " * 100, "runtime": {"type": "claude-code"}}}},
        {"id": 2, "role": "worker", "model": "backup-model", "metadata": {
        "profile": {"runtime": {"type": "claude-code"}}, "fallback_from": "primary-profile"}}]
    text = process_record_summary(process_record(task(), journal, runs))
    assert all(piece in text for piece in ("第 12 轮", "通过", "completed", "backup-model", "primary-profile"))
    assert "失败" not in text and "failed" not in text and len(text) <= 1200


def test_delivery_summary_keeps_final_adjudicated_seat_verdict():
    from looper.agents.workflow_reporting import process_record_summary

    plan = task(PlanStep('s1', 0, 'Build', key='s1'),
                PlanStep('r1', 1, 'Review', key='s1/review_a', reviews='s1',
                         result={'passed': True, 'status': 'passed_by_adjudication', 'findings': []}))
    journal = [entry(JournalKind.REVIEW_ROUND, 's1', round=0,
                     reviews={'s1/review_a': {'passed': False, 'findings': ['Blocking']}})]
    text = process_record_summary(process_record(plan, journal, []))
    assert '裁决后通过' in text and '失败' not in text


def test_unique_run_history_cannot_starve_other_sections_or_hide_omissions():
    runs = [{"id": i, "role": "worker", "model": f"model-{i}", "metadata": {
        "profile": {"name": "Profile " * 20, "runtime": {"type": "claude-code"}}}} for i in range(80)]
    data = process_record(task(), [entry(JournalKind.REVIEW_ROUND, round=0,
        reviews={"review_a": {"passed": True}}),
        entry(JournalKind.VERIFICATION_ROUND, attempt=1, head="checked", status="completed"),
        entry(JournalKind.ESCALATION, reason="Approval", gates=["g1"])], runs)
    text = process_record_text(data)
    kept = sum(item in text for items in data["sections"].values() for item in items)
    omitted = data["omitted"] + sum(map(len, data["sections"].values())) - kept
    assert omitted > 0 and f'已省略 {omitted} 条记录。' in text
    assert all(piece in text for piece in ("review_a", "checked", "Approval", SAFETY))
    assert len(text) <= 6000
    stored_entries = [item for items in data["sections"].values() for item in items]
    assert all(line in stored_entries for line in text.splitlines() if line.startswith('- '))
    assert len(json.dumps(data, ensure_ascii=False)) <= 60000
