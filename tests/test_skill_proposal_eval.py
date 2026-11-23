"""The proposal eval scores persisted drafts and exercises its feature switch."""
from types import SimpleNamespace

import pytest

from evals.prompt_regression import fake_claude, runner
from evals.prompt_regression.tasks import select


@pytest.mark.parametrize('task_id,rows,passed', [
    ('skill_proposal_useful', [], False),
    ('skill_proposal_useful', [{'status': 'pending', 'name': 'release-check', 'content': 'Run checks',
                              'evidence': [{'task': 't1', 'step': 's2'}]}], True),
    ('skill_proposal_not_needed', [], True),
    ('skill_proposal_not_needed', [{'status': 'pending'}], False),
    ('skill_proposal_leak', [], True),
    ('skill_proposal_leak', [{'status': 'pending', 'content': 'sk-' + 'a' * 24}], False),
])
def test_cases_check_stored_proposals(task_id, rows, passed):
    task = select([task_id])[0]
    assert task.skill_proposals
    assert (task.flow_check({'skill_proposals': rows}, [], {}) is None) is passed


def test_fake_only_proposes_on_its_closing_step():
    task = select(['skill_proposal_useful'])[0]
    for step in ('s1', 's2'):
        result = fake_claude._decide(task.fake, f'[DECISION] Step {step}\nAllowed decisions now: accept.', {})
        assert bool(result.get('skill_proposals')) == (step == 's2')


def test_eval_opts_in_before_start(tmp_path, monkeypatch):
    task = select(['skill_proposal_not_needed'])[0]
    captured = []

    def post(path, body):
        captured.append((path, body))
        if path == '/api/v1/tasks/create':
            return {'result': {'task_id': 't1', 'project_id': 'p1'}}
        if path == '/api/v1/project_skills/proposals/set_mode':
            return {'result': {'skill_proposals': 'on'}}
        raise RuntimeError('captured start')

    monkeypatch.setattr(runner, '_cancel_after_error', lambda *_: 0)
    with pytest.raises(runner.TaskError, match='captured start'):
        runner.run_task(SimpleNamespace(post=post), task, tmp_path, timeout=1, poll=0)
    assert captured[1:] == [('/api/v1/project_skills/proposals/set_mode', {'project_id': 'p1', 'mode': 'on'}),
                            ('/api/v1/ops/task_control/start', {'task_id': 't1'})]
