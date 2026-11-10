"""Dependency-condition evals score the persisted plan, and opt in explicitly."""
import json
from types import SimpleNamespace

import pytest

from evals.prompt_regression import fake_claude, runner
from evals.prompt_regression.tasks import EvalTask, select
from looper.agents.tiers.planner_protocol import parse_plan


@pytest.mark.parametrize('task_id', ['any_of_ready', 'conditions_not_needed'])
def test_condition_eval_fake_plan_and_workers_deliver_the_required_files(task_id, tmp_path, monkeypatch):
    task = select([task_id])[0]
    assert task.dependency_conditions
    parsed = parse_plan('```json\n' + json.dumps({'steps': task.fake['plan']}) + '\n```', conditions=True)
    assert len(parsed.steps) >= 2
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(fake_claude.time, 'sleep', lambda _: None)
    monkeypatch.setattr(fake_claude, '_answer', lambda *_: None)
    for item in task.fake['plan']:
        fake_claude._worker(task.fake, f"Your step: {item['id']}", SimpleNamespace(readline=lambda: 'start'), [])
    assert task.check(tmp_path) is None
    assert task.flow_check({'steps': [{**item, 'status': 'verified'} for item in task.fake['plan']]}, [], {}) is None


@pytest.mark.parametrize('task_id,condition,expected', [
    ('any_of_ready', {'any_of': ['a', 'b']}, True),
    ('any_of_ready', {'all_of': ['a', 'b']}, False),
    ('any_of_ready', None, False),
    ('conditions_not_needed', None, True),
    ('conditions_not_needed', {'all_of': ['a', 'b']}, True),
    ('conditions_not_needed', {'any_of': ['a', 'b']}, False),
    ('conditions_not_needed', {'k_of': 1, 'of': ['a', 'b']}, False),
    ('conditions_not_needed', {'k_of': 2, 'of': ['a', 'b']}, True),
])
def test_condition_eval_checks_actual_persisted_condition(task_id, condition, expected):
    task = select([task_id])[0]
    detail = {'steps': [{'id': 'a'}, {'id': 'b'}, {'id': 'c', 'depends_on': ['a', 'b'], 'ready_when': condition}]}
    assert (task.flow_check(detail, [], {}) is None) is expected


@pytest.mark.parametrize('enabled', [False, True])
def test_eval_enables_conditions_before_start_using_real_v1_request_schemas(enabled, tmp_path, monkeypatch):
    import jsonschema
    from looper.agents.v1_domains import projects, task_create

    task = EvalTask('project_settings', 'r', lambda _: None, dependency_conditions=enabled)
    captured = []
    schemas = {operation.path: operation.request for operation in (*task_create.OPERATIONS, *projects.OPERATIONS)}

    def post(path, body):
        captured.append((path, body))
        if path in schemas:
            jsonschema.validate(body, schemas[path])
        if path == '/api/v1/tasks/create':
            return {'result': {'task_id': 'task-1', 'project_id': 'project-1'}}
        if path == '/api/v1/project_admin/update':
            return {'result': {'project': {'dependency_conditions': 'on'}}}
        raise RuntimeError('captured start')

    monkeypatch.setattr(runner, '_cancel_after_error', lambda *_: 0)
    with pytest.raises(runner.TaskError, match='captured start'):
        runner.run_task(SimpleNamespace(post=post), task, tmp_path, timeout=1, poll=0)
    project = captured[0][1]['project']
    assert project == {'name': project['name'], 'work_dir': str(tmp_path), 'task_workspaces': 'in_place'}
    assert captured[1:] == ([('/api/v1/project_admin/update', {
        'project_id': 'project-1', 'fields': {'dependency_conditions': 'on'}})] if enabled else []) + [
            ('/api/v1/ops/task_control/start', {'task_id': 'task-1'})]


@pytest.mark.parametrize('condition,late,expected', [
    ({'any_of': ['a', 'b']}, '2026-10-13T10:00:08+00:00', {'edges': 2, 'verified': 1, 'violations': 0, 'unknown': 1}),
    ({'all_of': ['a', 'b']}, '2026-10-13T10:00:08+00:00', {'edges': 2, 'verified': 1, 'violations': 1, 'unknown': 0}),
    ({'any_of': ['a', 'b']}, None, {'edges': 2, 'verified': 1, 'violations': 0, 'unknown': 1}),
])
def test_condition_order_metric_allows_alternative_branch_to_keep_running(condition, late, expected):
    from evals.prompt_regression.metrics import dependency_order
    from tests.test_prompt_eval import _record
    steps = [{'id': 'a'}, {'id': 'b'}, {'id': 'c', 'depends_on': ['a', 'b'], 'ready_when': condition}]
    records = [_record(1, 'worker-a', step_id='a', end='2026-10-13T10:00:04+00:00'),
               _record(2, 'worker-b', step_id='b', end=late),
               _record(3, 'worker-c', step_id='c', start='2026-10-13T10:00:05+00:00')]
    assert dependency_order(steps, records) == expected


@pytest.mark.parametrize('on,expected', [('completed', 'verified'), ('verified', 'violations')])
def test_completed_order_metric_does_not_wait_for_the_reviewer(on, expected):
    from evals.prompt_regression.metrics import dependency_order
    from tests.test_prompt_eval import _record
    steps = [{'id': 'a'}, {'id': 'c', 'depends_on': ['a'],
                            'ready_when': {'step': 'a', 'on': on}}]
    records = [_record(1, 'worker-a', step_id='a', end='2026-10-13T10:00:04+00:00'),
               _record(2, 'reviewer-a', step_id='a', end='2026-10-13T10:00:08+00:00'),
               _record(3, 'worker-c', step_id='c', start='2026-10-13T10:00:05+00:00')]
    assert dependency_order(steps, records) == {'edges': 1, 'verified': int(expected == 'verified'),
                                                'violations': int(expected == 'violations'), 'unknown': 0}
