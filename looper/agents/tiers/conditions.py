"""Pure parsing, rendering and three-valued evaluation of step dependencies."""
from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import Enum
from typing import Any


class On(str, Enum):
    COMPLETED = 'completed'
    VERIFIED = 'verified'
    PASSED = 'passed'


class State(str, Enum):
    SATISFIED = 'satisfied'
    PENDING = 'pending'
    UNSATISFIABLE = 'unsatisfiable'


@dataclass(frozen=True)
class Leaf:
    step: str
    on: On = On.VERIFIED
    shorthand: bool = True


@dataclass(frozen=True)
class KOf:
    k: int
    of: tuple[Condition, ...]
    form: str = 'k_of'


Condition = Leaf | KOf
_SHAPE = ('use a step string, {"step": "s1", "on": "completed|verified|passed"}, '
          '{"all_of": [...]}, {"any_of": [...]}, or {"k_of": 2, "of": [...]}')


def parse(value: Any, where: str) -> Condition:
    """Validate a condition and retain its original JSON spelling."""
    from .planner_protocol import ProtocolError

    count = 0

    def invalid(reason: str) -> None:
        raise ProtocolError(f'{where}: {reason}; correct shape: {_SHAPE}')

    def walk(raw: Any, depth: int) -> Condition:
        nonlocal count
        if isinstance(raw, str) or isinstance(raw, dict) and set(raw) == {'step', 'on'}:
            step = raw if isinstance(raw, str) else raw['step']
            if not isinstance(step, str) or not step.strip():
                invalid('step must be a non-empty string')
            try:
                on = On.VERIFIED if isinstance(raw, str) else On(raw['on'])
            except (TypeError, ValueError):
                invalid('on must be completed, verified or passed')
            count += 1
            if count > 32:
                invalid('at most 32 leaves are allowed')
            return Leaf(step, on, isinstance(raw, str))
        if not isinstance(raw, dict):
            invalid('invalid condition shape')
        if set(raw) == {'all_of'} or set(raw) == {'any_of'}:
            form = next(iter(raw))
            children, k = raw[form], None
        elif set(raw) == {'k_of', 'of'}:
            form, children, k = 'k_of', raw['of'], raw['k_of']
        else:
            invalid('invalid condition shape')
        if depth >= 4:
            invalid('nesting must not exceed 4 groups')
        if not isinstance(children, list) or not children:
            invalid('groups must contain a non-empty list')
        if form == 'k_of' and (type(k) is not int or not 1 <= k <= len(children)):
            invalid('k_of must be a whole number in 1..the number of children')
        parsed = tuple(walk(child, depth + 1) for child in children)
        seen: set[str] = set()
        for child in parsed:
            ids = leaves(child)
            if seen & ids:
                invalid('duplicate step in the same group')
            seen.update(ids)
        return KOf(len(parsed) if form == 'all_of' else 1 if form == 'any_of' else k, parsed, form)

    return walk(value, 0)


def leaves(cond: Condition) -> frozenset[str]:
    return frozenset({cond.step}) if isinstance(cond, Leaf) else frozenset().union(*(leaves(c) for c in cond.of))


def to_json(cond: Condition, rename: Callable[[str], str] | Mapping[str, str]) -> Any:
    name = rename if callable(rename) else rename.__getitem__
    if isinstance(cond, Leaf):
        step = name(cond.step)
        return step if cond.shorthand and cond.on is On.VERIFIED else {'step': step, 'on': cond.on.value}
    children = [to_json(child, name) for child in cond.of]
    return {'k_of': cond.k, 'of': children} if cond.form == 'k_of' else {cond.form: children}


def render(cond: Condition) -> str:
    if isinstance(cond, Leaf):
        return cond.step + ('' if cond.on is On.VERIFIED else f' {cond.on.value}')
    prefix = {'all_of': 'all of', 'any_of': 'any of'}.get(cond.form, f'{cond.k} of')
    return prefix + ' (' + ', '.join(render(child) for child in cond.of) + ')'


@dataclass(frozen=True)
class StepOutcome:
    completed: State = State.PENDING
    verified: State = State.PENDING
    passed: State = State.PENDING
    reason: str = ''

    def for_on(self, on: On) -> State:
        return getattr(self, on.value)

    @classmethod
    def from_step(cls, step: Any) -> StepOutcome:
        """Read a PlanStep (or equivalent projection) without importing the Planner."""
        if step is None:
            return cls(*(State.UNSATISFIABLE,) * 3, reason='step no longer exists')
        status = step.status
        if status in ('failed', 'skipped'):
            return cls(*(State.UNSATISFIABLE,) * 3, reason=f'step is {status}')
        if getattr(step, 'kind', None) == 'review':
            if status != 'verified':
                return cls()
            result = getattr(step, 'result', None) or {}
            passed = result.get('passed') is True or result.get('status') == 'passed_by_adjudication'
            return cls(State.SATISFIED, State.SATISFIED,
                       State.SATISFIED if passed else State.UNSATISFIABLE,
                       'review passed' if passed else 'review did not pass')
        gate, wait = getattr(step, 'gate', None), getattr(step, 'wait', None)
        if gate is not None:
            if gate.state in ('passed', 'cancelled'):
                passed = State.SATISFIED if gate.state == 'passed' else State.UNSATISFIABLE
                return cls(State.SATISFIED, State.SATISFIED, passed, f'gate is {gate.state.value if isinstance(gate.state, Enum) else gate.state}')
            return cls()
        if wait is not None:
            if wait.state == 'done':
                passed = State.SATISFIED if wait.outcome in ('ready', 'resumed') else State.UNSATISFIABLE
                outcome = wait.outcome.value if isinstance(wait.outcome, Enum) else wait.outcome
                return cls(State.SATISFIED, State.SATISFIED, passed, f'wait ended with {outcome}')
            return cls()
        if status == 'verified':
            return cls(*(State.SATISFIED,) * 3)
        if status == 'completed' or status == 'deciding' and (getattr(step, 'result', None) or {}).get('status') == 'completed':
            return cls(completed=State.SATISFIED)
        return cls()


def evaluate(cond: Condition, outcome: Callable[[str], StepOutcome]) -> State:
    if isinstance(cond, Leaf):
        return outcome(cond.step).for_on(cond.on)
    states = [evaluate(child, outcome) for child in cond.of]
    satisfied = states.count(State.SATISFIED)
    if satisfied >= cond.k:
        return State.SATISFIED
    if satisfied + states.count(State.PENDING) < cond.k:
        return State.UNSATISFIABLE
    return State.PENDING
