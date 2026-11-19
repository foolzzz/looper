"""The message stream adds no model-visible content: no agent's tools or prompts mention it, with or without a source."""

import pytest

from looper.agents.ops import clients, planner, room, service
from looper.agents.tiers import manager, planner as planner_tier, worker

TERMS = ("message_stream", "msgflow", "message stream", "source_events", "event_sources")


def test_no_agent_tool_or_default_prompt_mentions_the_message_stream():
    tools = (*service.MANAGER_TOOLS, *clients.CLIENT_TOOLS, *planner.TOOLS, *planner.ROLE_TOOLS, *room.TOOLS)
    texts = [f"{tool.name} {tool.description} {tool.schema}" for tool in tools]
    texts += [manager.DEFAULT_SYSTEM_PROMPT, planner_tier.DEFAULT_SYSTEM_PROMPT, worker.DEFAULT_SYSTEM_PROMPT]
    assert [text[:60] for text in texts if any(term in text.lower() for term in TERMS)] == []


@pytest.mark.parametrize("name", ["message_stream.status", "message_stream.runs", "message_stream.configure"])
def test_the_operations_are_operator_dashboard_operations_not_agent_tools(name):
    from looper.agents import api_v1

    operation = next(op for op in api_v1.operations() if op.operation_id == name)
    assert operation.operator_only and operation.tool is None
