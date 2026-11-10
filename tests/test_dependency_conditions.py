"""Conditions preserve their syntax and evaluate typed step outcomes."""
from types import SimpleNamespace

import pytest

from looper.agents.tiers.conditions import On, State, StepOutcome, evaluate, leaves, parse, render, to_json
from looper.agents.tiers.planner_protocol import ProtocolError


@pytest.mark.parametrize(('shape', 'states', 'expected'), [
    ({'all_of': ['a', 'b']}, ['satisfied', 'satisfied'], 'satisfied'),
    ({'all_of': ['a', 'b']}, ['satisfied', 'pending'], 'pending'),
    ({'all_of': ['a', 'b']}, ['pending', 'unsatisfiable'], 'unsatisfiable'),
    ({'any_of': ['a', 'b']}, ['satisfied', 'pending'], 'satisfied'),
    ({'any_of': ['a', 'b']}, ['pending', 'unsatisfiable'], 'pending'),
    ({'any_of': ['a', 'b']}, ['unsatisfiable', 'unsatisfiable'], 'unsatisfiable'),
    ({'k_of': 2, 'of': ['a', 'b', 'c']}, ['satisfied', 'satisfied', 'pending'], 'satisfied'),
    ({'k_of': 2, 'of': ['a', 'b', 'c']}, ['satisfied', 'pending', 'unsatisfiable'], 'pending'),
    ({'k_of': 2, 'of': ['a', 'b', 'c']}, ['pending', 'unsatisfiable', 'unsatisfiable'], 'unsatisfiable'),
])
def test_combination_truth_table(shape, states, expected):
    outcomes = {key: StepOutcome(verified=State(state)) for key, state in zip('abc', states)}
    assert evaluate(parse(shape, 'ready_when'), outcomes.__getitem__) == State(expected)


@pytest.mark.parametrize(('status', 'result', 'gate', 'wait', 'expected'), [
    ('pending', None, None, None, ('pending', 'pending', 'pending')),
    ('pending', {'status': 'completed'}, None, None, ('pending',) * 3),
    ('dispatched', {'status': 'completed'}, None, None, ('pending',) * 3),
    ('completed', {'status': 'completed'}, None, None, ('satisfied', 'pending', 'pending')),
    ('deciding', {'status': 'completed'}, None, None, ('satisfied', 'pending', 'pending')),
    ('verified', {'status': 'completed'}, None, None, ('satisfied', 'satisfied', 'satisfied')),
    ('failed', {'status': 'failed'}, None, None, ('unsatisfiable',) * 3),
    ('skipped', None, None, None, ('unsatisfiable',) * 3),
    ('verified', None, 'passed', None, ('satisfied',) * 3),
    ('verified', None, 'cancelled', None, ('satisfied', 'satisfied', 'unsatisfiable')),
    ('waiting_user', None, 'open', None, ('pending',) * 3),
    ('verified', None, None, ('done', 'ready'), ('satisfied',) * 3),
    ('verified', None, None, ('done', 'resumed'), ('satisfied',) * 3),
    ('verified', None, None, ('done', 'cancelled'), ('satisfied', 'satisfied', 'unsatisfiable')),
    ('verified', None, None, ('done', 'proceeded'), ('satisfied', 'satisfied', 'unsatisfiable')),
    ('waiting', None, None, ('ready', 'ready'), ('pending',) * 3),
])
def test_step_outcome_truth_table(status, result, gate, wait, expected):
    step = SimpleNamespace(status=status, result=result,
                           gate=SimpleNamespace(state=gate) if gate else None,
                           wait=SimpleNamespace(state=wait[0], outcome=wait[1]) if wait else None)
    outcome = StepOutcome.from_step(step)
    for on, state in zip(On, expected):
        assert evaluate(parse({'step': 'a', 'on': on.value}, 'condition'), lambda _: outcome) == State(state)


def test_review_outcomes_remain_pending():
    step = SimpleNamespace(kind='review', status='verified', gate=None, wait=None)
    assert StepOutcome.from_step(step) == StepOutcome()


def test_nested_conditions_preserve_shape_and_rename():
    shape = {'any_of': [{'all_of': ['a', {'step': 'b', 'on': 'completed'}]},
                        {'k_of': 1, 'of': [{'step': 'c', 'on': 'verified'}]}]}
    condition = parse(shape, 'ready_when')
    assert leaves(condition) == frozenset({'a', 'b', 'c'})
    assert to_json(condition, lambda key: key) == shape
    assert to_json(parse('a', 'condition'), {'a': 'step-1'}) == 'step-1'
    assert render(condition) == 'any of (all of (a, b completed), 1 of (c))'


@pytest.mark.parametrize(('shape', 'error'), [
    ({'all_of': ['a', 'a']}, 'duplicate'),
    ({'any_of': ['a', {'step': 'a', 'on': 'passed'}]}, 'duplicate'),
    ({'k_of': 2, 'of': ['a']}, '1..'),
    ({'k_of': True, 'of': ['a']}, '1..'),
    ({'all_of': []}, 'non-empty'),
    ({'step': 'a', 'on': 'accepted'}, 'completed'),
    ({'step': '' , 'on': 'passed'}, 'step'),
    ({'step': 'a'}, 'shape'),
    ({'all_of': ['a'], 'any_of': ['b']}, 'shape'),
    (None, 'shape'),
    ({'all_of': [str(i) for i in range(33)]}, '32'),
    ({'all_of': [{'any_of': [{'all_of': [{'any_of': [{'all_of': ['a']}]}]}]}]}, '4'),
])
def test_rejects_invalid_condition(shape, error):
    with pytest.raises(ProtocolError, match=error) as caught:
        parse(shape, 'steps[2].ready_when')
    assert 'steps[2].ready_when' in str(caught.value)
    assert 'all_of' in str(caught.value)


def test_four_groups_and_thirty_two_leaves_are_allowed():
    shape = {'all_of': [{'all_of': [{'all_of': [{'all_of': [str(i) for i in range(32)]}]}]}]}
    assert len(leaves(parse(shape, 'condition'))) == 32
