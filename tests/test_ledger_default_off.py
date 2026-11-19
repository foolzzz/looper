"""The ledger adds nothing a model sees: no agent's tools or default prompts mention it, and a run's launch differs
from before only by the variable that marks it an agent's."""

import pytest

from looper.agents import agent_settings
from looper.agents.ops import clients, planner, room, service
from looper.agents.tiers import manager, planner as planner_tier, worker
from tests.runtime_model_helpers import builtin_launch

TERMS = ("ledger", "looper_ledger")


def test_no_agent_tool_or_default_prompt_mentions_the_ledger():
    tools = (*service.MANAGER_TOOLS, *clients.CLIENT_TOOLS, *planner.TOOLS, *planner.ROLE_TOOLS, *room.TOOLS)
    texts = [f"{tool.name} {tool.description} {tool.schema}" for tool in tools]
    texts += [manager.DEFAULT_SYSTEM_PROMPT, planner_tier.DEFAULT_SYSTEM_PROMPT, worker.DEFAULT_SYSTEM_PROMPT,
              agent_settings.DEFAULT_HARNESS_PROMPT]
    assert [text[:60] for text in texts if any(term in text.lower() for term in TERMS)] == []


@pytest.mark.parametrize("name", ["ledger.list", "ledger.get"])
def test_the_reads_are_not_agent_tools(name):
    from looper.agents import api_v1

    operation = next(op for op in api_v1.operations() if op.operation_id == name)
    assert operation.tool is None and operation.risk == "read" and not operation.operator_only


@pytest.mark.parametrize("role", list(agent_settings.AgentType) + [None])
def test_a_launch_differs_only_by_the_agent_run_marker(role):
    launch = builtin_launch(role=role)
    plain = launch.runtime.environment(launch)
    marked = launch.environment()
    assert "LOOPER_AGENT_RUN" not in plain
    assert marked == {**plain, "LOOPER_AGENT_RUN": role.value if role else "agent"}
    assert launch.args() == launch.runtime.launch_args(launch)
