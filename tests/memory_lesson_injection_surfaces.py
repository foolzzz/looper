"""Concrete model surfaces, recorded individually from the fixed pre-injection source."""
import hashlib
import json
from pathlib import Path


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()


def surfaces():
    from looper.agents import system_prompt as prompt
    from looper.agents.agent_settings import Harness, Layer, Boundaries
    from looper.agents.knowledge import AgentKnowledge
    from looper.agents.runtimes import adapter
    from looper.agents.task_roles import TaskRole, RoleKind
    from looper.agents.tiers.planner import PlannerAgent, PlannerSettings, TaskPlan, PlanStep
    from looper.agents.tiers.planner_protocol import reply_contract, DecisionContext, ResultKind
    from looper.agents.ops.service import MANAGER_TOOLS
    from looper.agents.ops.planner import TOOLS, ROLE_TOOLS

    agent = PlannerAgent(task_id='synthetic-task', runtime_root=Path('.'))
    plan = TaskPlan('synthetic-plan', 'synthetic-goal', 'Inspect synthetic data.', '')
    step = PlanStep('synthetic-step', 0, 'Check data', key='s1')
    plan.steps.append(step)
    result = {'planner:plan': agent._plan_message(plan), 'planner:resume': agent._resume_brief(plan),
              'planner:decision': agent._decision_message(plan, step,
                  DecisionContext('s1', ResultKind.WORK, True, False, 0), ['Checked.']),
              'planner:system': agent._system_prompt(plan, PlannerSettings('Plan carefully.', 0, 0, 0),
                  AgentKnowledge(), Harness('Work carefully.', Layer.BUILTIN)).text}
    for owner, tools in (('manager', MANAGER_TOOLS), ('planner', (*TOOLS, *ROLE_TOOLS))):
        result[f'catalog:{owner}:order'] = [tool.name for tool in tools]
        for tool in tools:
            result[f'catalog:{owner}:{tool.name}'] = tool.listing()
    for options in ({}, {'roles': True}, {'gates': True}, {'room': True}, {'conditions': True},
                    {'advisor': True}, {'skill_proposals': True}, {'review_panel': True}):
        result['planner:contract:' + json.dumps(options, sort_keys=True)] = reply_contract(**options)
    for runtime in ('claude-code', 'codex'):
        result['worker:contract:' + runtime] = prompt.work_contract(adapter(runtime))
    for severity in (False, True):
        for evidence in (False, True):
            result[f'reviewer:contract:{severity}:{evidence}'] = prompt.review_contract(severity, evidence=evidence)
    for role in ('worker', 'reviewer'):
        contract = prompt.work_contract(adapter('claude-code')) if role == 'worker' else prompt.review_contract()
        args = dict(harness=Harness('Work carefully.', Layer.BUILTIN), contract_name=role, contract=contract,
                    project={'id': 'synthetic-project', 'name': 'Synthetic'}, task='Inspect the synthetic data.',
                    room='## Room\nSynthetic room.', vault='## Knowledge vault\nSynthetic knowledge.')
        for kind in ('builtin', 'defined'):
            composed = (prompt.build_system_prompt(role_id=role, role='Inspect carefully.', recommended=False, **args)
                        if kind == 'builtin' else prompt.build_role_prompt(
                            boundaries=Boundaries('Respect the task scope.'),
                            task_role=TaskRole('inspector', RoleKind.WORK if role == 'worker' else RoleKind.REVIEW,
                                               1, 'Inspect carefully.'), **args))
            result[f'{role}:{kind}:prompt'] = composed.text
            for name, layer in composed.layers.items():
                result[f'{role}:{kind}:layer:{name}'] = layer
    return result
