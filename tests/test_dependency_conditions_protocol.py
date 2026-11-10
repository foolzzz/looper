"""Dependency conditions at the Planner reply and project configuration boundaries."""

import hashlib
import json
from dataclasses import replace

import pytest

from looper.agents.project_execution import ProjectExecution, execution_fields
from looper.agents.tiers.planner_protocol import (
    PlanView, ProtocolError, parse_chat, parse_plan, reply_contract,
)
from looper.agents.user_gates import GatePolicy


def reply(data):
    return '```json\n' + json.dumps(data) + '\n```'


def work(label, dependencies=(), **changes):
    return {"id": label, "instruction": "Complete the work", "acceptance_criteria": ["Checked"],
            "depends_on": list(dependencies), "review": False, **changes}


@pytest.mark.parametrize("target", [
    work("s2", ["s1"], ready_when="s1"),
    {"id": "g1", "kind": "gate", "topic": "Choose", "agenda": [{"question": "Which option?"}],
     "depends_on": ["s1"], "ready_when": "s1"},
    {"id": "w1", "kind": "wait", "topic": "Pause", "max_wait_seconds": 60, "not_before_seconds": 1,
     "depends_on": ["s1"], "ready_when": "s1"},
])
def test_condition_is_accepted_for_each_step_kind_when_enabled(target):
    result = parse_plan(reply({"steps": [work("s1"), target]}), conditions=True, gate_policy=GatePolicy.ALLOWED)
    assert result.steps[1].ready_when is not None


def test_conditions_are_refused_with_an_explicit_project_setting_message():
    with pytest.raises(ProtocolError, match="dependency_conditions is off"):
        parse_plan(reply({"steps": [work("s1", ready_when="s1")]}))


def test_condition_leaves_must_equal_the_declared_edges():
    with pytest.raises(ProtocolError, match="depends_on.*exactly"):
        parse_plan(reply({"steps": [work("s1"), work("s2", ["s1"], ready_when="other")]}), conditions=True)


def test_chat_add_steps_uses_the_plan_condition_permission():
    view = PlanView(started={"s1": ()}, pending={}, conditions=True)
    data = reply({"reply": "Continue", "action": "add_steps", "steps": [work("s2", ["s1"], ready_when="s1")]})
    assert parse_chat(data, view).steps[0].ready_when is not None
    with pytest.raises(ProtocolError, match="dependency_conditions is off"):
        parse_chat(data, replace(view, conditions=False))


def test_default_contract_bytes_are_unchanged():
    assert hashlib.sha256(reply_contract().encode()).hexdigest() == "b70ffeae8558158dbf5c765d6b8ffe9fe8ef425d7aede3a1e9ce6f5af5adb588"
    assert "any_of" not in reply_contract()


def test_enabled_contract_explains_conditions_and_alternate_branches():
    text = reply_contract(conditions=True)
    for phrase in ("all_of", "any_of", "k_of", "completed", "verified", "passed", "continue running"):
        assert phrase in text


@pytest.mark.parametrize(("value", "enabled"), [(None, False), ("off", False), ("on", True)])
def test_project_conditions_are_off_by_default(value, enabled):
    execution = ProjectExecution.from_row({"dependency_conditions": value})
    assert execution.conditions_on is enabled
    assert execution.view()["dependency_conditions"] == value
    assert execution_fields({"dependency_conditions": value}) == {"dependency_conditions": value}


def test_project_rejects_an_invalid_condition_permission():
    with pytest.raises(ValueError, match="dependency_conditions"):
        execution_fields({"dependency_conditions": "sometimes"})


def test_replan_cannot_change_a_started_steps_condition():
    view = PlanView(started={"s1": (), "s2": ("s1",)}, pending={}, conditions=True)
    data = reply({"reply": "Replace", "action": "replan", "steps": [work("s2", ["s1"], ready_when="s1")]})
    with pytest.raises(ProtocolError, match="started steps cannot change"):
        parse_chat(data, view)


def test_replan_cannot_reference_a_dropped_condition_leaf():
    view = PlanView(started={"s1": ()}, pending={}, retired=frozenset({"old"}), conditions=True)
    data = reply({"reply": "Replace", "action": "replan", "steps": [work("s2", ["old"], ready_when="old")]})
    with pytest.raises(ProtocolError, match="old, which is dropped"):
        parse_chat(data, view)


@pytest.mark.parametrize("entry", ["plan", "add_steps", "replan", "decision"])
def test_worktree_condition_policy_applies_to_every_new_steps_entry(entry):
    from looper.agents.tiers.planner_protocol import DecisionContext, ResultKind, parse_decision

    steps = [work("s2", ["s1"], ready_when={"step": "s1", "on": "completed"})]
    view = PlanView(started={"s1": ()}, pending={}, conditions=True, isolate_steps=True)
    with pytest.raises(ProtocolError, match="in worktree isolation, wait for verified"):
        if entry == "plan":
            parse_plan(reply({"steps": [work("s1"), *steps]}), conditions=True, isolate_steps=True)
        elif entry == "decision":
            context = DecisionContext("s1", ResultKind.WORK, True, False, 0)
            parse_decision(reply({"decision": "replan", "reason": "Continue", "steps": steps}), context, view)
        else:
            parse_chat(reply({"reply": "Continue", "action": entry, "steps": steps}), view)


def test_database_journal_kinds_cover_the_typed_lifecycle():
    from looper.agents.tiers.journal import JournalKind
    from looper.db.journal import KINDS

    assert set(KINDS) == {kind.value for kind in JournalKind}


def test_unconditional_pipeline_tool_output_keeps_the_released_bytes(monkeypatch):
    import asyncio
    from datetime import datetime, timezone
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from looper.agents.ops.clients import PIPELINE_TOOL
    from looper.db import tasks, user_gates, waits

    monkeypatch.setattr(tasks, "get_task_with_steps", AsyncMock(return_value={
        "id": "task-baseline", "name": "Build", "status": "waiting", "phase": "waiting",
        "steps": [
            {"id": "step-dev", "step_key": "dev", "status": "verified", "instruction": "Implement",
             "ready_when": None},
            {"id": "step-wait", "step_key": "w1", "kind": "wait", "status": "waiting",
             "depends_on": ["step-dev"], "instruction": "Wait for checks", "ready_when": None},
        ],
    }))
    monkeypatch.setattr(user_gates, "list_for_task", AsyncMock(return_value=[]))
    monkeypatch.setattr(waits, "list_for_task", AsyncMock(return_value=[{
        "step_id": "step-wait", "step_key": "w1", "state": "waiting", "topic": "Wait for checks",
        "spec": {"every_seconds": 600}, "depends_on": ["step-dev"], "ready_when": None,
        "opened_at": datetime(2026, 10, 20, 12, tzinfo=timezone.utc),
    }]))
    result = asyncio.run(PIPELINE_TOOL.run(SimpleNamespace(arguments={"task_id": "task-baseline"})))
    expected = {
        "task_id": "task-baseline", "name": "Build", "status": "waiting", "group": "in_progress",
        "phase": "waiting",
        "steps": [
            {"step": "dev", "kind": "work", "status": "verified", "depends_on": [], "instruction": "Implement"},
            {"step": "w1", "kind": "wait", "status": "waiting", "depends_on": ["step-dev"],
             "instruction": "Wait for checks"},
        ],
        "gates": [],
        "waits": [{
            "step": "w1", "step_id": "step-wait", "state": "waiting", "outcome": None,
            "topic": "Wait for checks", "check": None, "every_seconds": 600,
            "opened_at": "2026-10-20T12:00:00+00:00", "due_at": None, "deadline_at": None,
            "next_check_at": None, "last_checked_at": None, "last_result": None, "notice": None,
            "closed_at": None, "checks": 0, "last_output": "", "last_error": "", "resolution": "",
            "depends_on": ["step-dev"],
        }],
    }
    assert json.dumps(result) == json.dumps(expected)


@pytest.mark.parametrize('condition', [None, 's1'])
def test_wait_check_trigger_and_dependency_condition_remain_distinct(condition):
    from looper.agents.tiers.conditions import Leaf
    from looper.agents.waits import ReadyWhen

    wait = {'id': 'w1', 'kind': 'wait', 'topic': 'Author updates the change', 'depends_on': ['s1'],
            'max_wait_seconds': 600,
            'check': {'type': 'github_pr', 'repo': 'example-org/demo', 'number': 1, 'ready_when': 'head_changed'}}
    if condition is not None:
        wait['ready_when'] = condition
    result = parse_plan(reply({'steps': [work('s1'), wait]}), conditions=condition is not None).steps[1]
    assert result.ready_when == (Leaf('s1') if condition is not None else None)
    assert result.wait.check.ready_when is ReadyWhen.HEAD_CHANGED
