"""Planner feature wiring and frozen off-path messages from main."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from looper.agents import skill_proposal_sources
from looper.agents.agent_settings import Harness as PromptHarness, Layer
from looper.agents.knowledge import AgentKnowledge
from looper.agents.tiers import planner as module
from looper.agents.tiers.planner_protocol import (
    DecisionContext, ProtocolError, ResultKind, TurnKind, parse_decision, parse_plan,
)
WORK_OK = {"status": "completed", "summary": "checks passed"}
DRAFT = {"action": "create", "name": "release-check", "description": "Before a release",
         "content": "Run the checks.", "why": "The procedure repeated and holds for future tasks."}
FINAL = {key: "done" for key in ("delivered", "how_to_use", "verification", "open_issues")}


@pytest.mark.parametrize('name,expected', [
    ('release-check', 'release-check'), (' release-check ', 'release-check'),
    (None, '(draft)'), ('Not a slug', '(draft)'), ('x' * 65, '(draft)'),
    ('AKIA' + 'A1B2' * 4, '(draft)'), ('sk-' + 'x' * 30, '(draft)'),
])
def test_safe_name_keeps_only_valid_nonsecret_slugs(name, expected):
    assert skill_proposal_sources._safe_name({'name': name}) == expected


def _reply(data):
    return '```json\n' + json.dumps(data) + '\n```'


FIXTURE = Path(__file__).with_name('fixtures') / 'planner_messages_main.json'
BASELINE_SHA = '6613380fa48d7e7d2a7103f53ad84621d9c5efb5'


def snapshot(planner_module=module):
    """Stable product inputs, also used to capture the explicitly named main commit."""
    agent = planner_module.PlannerAgent(task_id='task-fixture', runtime_root=Path('/tmp/runtime-fixture'))
    item = planner_module.PlanStep('step-fixture', 0, 'Run the checks', key='s1', status='deciding',
                                   worker_id='worker-fixture', result=WORK_OK)
    task = planner_module.TaskPlan('plan-fixture', 'goal-fixture', 'Check this project', '/tmp/project-fixture',
                                   steps=[item], status='executing', project_id='project-fixture',
                                   project_name='Example', project_background='Use the project checks.')
    agent._plan = task
    context = DecisionContext('s1', ResultKind.WORK, True, False, 0, last_open=True)
    settings = planner_module.PlannerSettings('Plan carefully.', 60, 0, 0, recommended=False)
    return {
        'plan': agent._plan_message(task),
        'decision': agent._decision_message(task, item, context, agent._work_report(item, WORK_OK)),
        'system': agent._system_prompt(task, settings, AgentKnowledge(), PromptHarness('', Layer.BUILTIN)).text,
    }


def test_off_messages_and_system_prompt_match_frozen_main():
    fixture = json.loads(FIXTURE.read_text())
    assert fixture['commit'] == BASELINE_SHA
    assert snapshot() == fixture['messages']


@pytest.mark.parametrize('mode', [None, 'off', 'on'])
def test_refreshed_project_switch_reaches_contract_and_checked_parser(mode, agent_db):
    async def run():
        agent = module.PlannerAgent(task_id='task-fixture', runtime_root=Path('/tmp/runtime-fixture'))
        task = module.TaskPlan('plan-fixture', 'goal-fixture', 'Check project', '/tmp/project-fixture')
        agent_db.get_task_project.return_value = {'id': 'project-fixture', 'name': 'Example', 'skill_proposals': mode}
        await agent._refresh_project(task)
        agent._plan = task
        settings = module.PlannerSettings('Plan carefully.', 60, 0, 0, recommended=False)
        prompt = agent._system_prompt(task, settings, AgentKnowledge(), PromptHarness('', Layer.BUILTIN)).text
        assert ('may also carry skill_proposals' in prompt) is (mode == 'on')
        view = agent._checked(module.PlanView({'s1': ()}, {}))
        context = DecisionContext('s1', ResultKind.WORK, True, False, 0, last_open=True)
        text = _reply({'decision': 'accept', 'final': FINAL, 'skill_proposals': [DRAFT]})
        if mode == 'on':
            assert parse_decision(text, context, view).skill_proposals[0].to_dict() == DRAFT
        else:
            with pytest.raises(ProtocolError, match='unknown keys: skill_proposals'):
                parse_decision(text, context, view)
    asyncio.run(run())


@pytest.mark.parametrize('enabled', [False, True])
def test_actual_ask_adds_feedback_only_to_opted_in_plan_turn(enabled, agent_db, monkeypatch):
    async def run():
        agent = module.PlannerAgent(task_id='task-fixture', runtime_root=Path('/tmp/runtime-fixture'))
        task = module.TaskPlan('plan-fixture', 'goal-fixture', 'Check project', '/tmp/project-fixture')
        agent._plan = task
        agent_db.get_task_project.return_value = {'id': 'project-fixture', 'skill_proposals': 'on' if enabled else None}
        await agent._refresh_project(task)
        note = AsyncMock(side_effect=[['skill proposal p-12 release-check: confirmed'], []])
        monkeypatch.setattr(skill_proposal_sources, 'planner_note', note)
        plan_text = _reply({'steps': [{'id': 's1', 'instruction': 'Run checks', 'acceptance_criteria': ['passes'],
                                      'depends_on': [], 'review': False}]})
        closing = _reply({'decision': 'accept', 'final': FINAL,
                         **({'skill_proposals': [DRAFT]} if enabled else {})})
        stand_in = AsyncMock(side_effect=[plan_text, closing, plan_text])
        first = agent._plan_message(task)
        await agent._ask(TurnKind.PLAN, first, parse_plan, run=stand_in)
        context = DecisionContext('s1', ResultKind.WORK, True, False, 0, last_open=True)
        decision = '[DECISION] Check the last step.'
        parsed = await agent._ask(TurnKind.DECISION, decision,
                                 lambda text: parse_decision(text, context, agent._checked(module.PlanView({'s1': ()}, {}))),
                                 run=stand_in)
        await agent._ask(TurnKind.PLAN, first, parse_plan, run=stand_in)
        sent = [call.args[0] for call in stand_in.await_args_list]
        assert sent[0] == first + ('\n\nskill proposal p-12 release-check: confirmed' if enabled else '')
        assert sent[1] == decision and sent[2] == first
        assert [draft.to_dict() for draft in parsed.skill_proposals] == ([DRAFT] if enabled else [])
        assert parsed.final is not None
        if enabled:
            assert note.await_count == 2
            note.assert_awaited_with('project-fixture', claimed=[])
        else:
            note.assert_not_awaited()
    asyncio.run(run())
