"""The ledger adds nothing a model sees: no agent's tools or default prompts mention it, and a run's launch differs
from before only by the variable that marks it an agent's."""

from dataclasses import asdict
import hashlib
import json
from pathlib import Path

import pytest

from looper.agents import agent_settings
from looper.agents.ops import clients, planner, room, service
from looper.agents.tiers import manager, planner as planner_tier, worker
from tests.prompt_sections import assert_sections

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


def test_dashboard_operations_are_not_agent_or_cli_tools():
    from looper import loopercli
    from looper.agents import api_v1
    from looper.agents.action_levels import ActionLevel

    document = api_v1.document()
    by_id = {operation.operation_id: operation for operation in api_v1.operations()}
    tools = (*service.MANAGER_TOOLS, *clients.CLIENT_TOOLS, *planner.TOOLS, *planner.ROLE_TOOLS, *room.TOOLS)
    for name, risk in (("ledger.page", "read"), ("ledger.add", "write"), ("ledger.action", "write")):
        operation = by_id[name]
        assert operation.risk == risk and operation.operator_only and operation.cli is False
        assert operation.level is ActionLevel.INSIDE and operation.level_of is None and operation.tool is None
        shown = document["paths"][operation.path.removeprefix(api_v1.PREFIX)]["post"]
        assert shown["x-looper-cli"] is False and shown["x-looper-operator-only"] is True
        assert "x-looper-tool" not in shown
        assert name not in {tool.name for tool in tools}
    # Recorded from 2975651b514ac28d2826b768cea2e60171a21b7c, one command at a time. Dashboard
    # operations cannot add a command or change either existing read command.
    frozen = {"list": "9ca58a6aa74f7d0b01ae90d37c8107a509173af03b13274479dc4d3b1d05ec86",
              "get": "5391a24a73b170f402de9d0a432bddf8878fa58d12d7ec1815cf8eb875866c7e"}
    commands = {}
    for command, sub, summary, description, generated in loopercli.generated_operations(document):
        if command == "ledger":
            commands[sub] = _sha([command, sub, summary, description, asdict(generated)])
    assert commands == frozen
    for name in ("ledger.list", "ledger.get"):
        assert not by_id[name].operator_only and by_id[name].risk == "read" and by_id[name].cli

    # Reuse the committed per-section/per-tool baseline; no moving git ref or
    # whole-module digest can hide a changed model-visible item.
    baseline = json.loads((Path(__file__).parent / "fixtures/catalog_default_off.json").read_text())
    prompts = {"manager": manager.DEFAULT_SYSTEM_PROMPT, "planner": planner_tier.DEFAULT_SYSTEM_PROMPT,
               "worker": worker.DEFAULT_SYSTEM_PROMPT, "harness": agent_settings.DEFAULT_HARNESS_PROMPT}
    for name, text in prompts.items():
        assert_sections(text, baseline["prompts"][name], label=name)
    catalogs = {"manager": service.MANAGER_TOOLS, "client": clients.CLIENT_TOOLS, "planner": planner.TOOLS,
                "planner_roles": planner.ROLE_TOOLS, "room": room.TOOLS}
    for name, catalog in catalogs.items():
        frozen_tools = baseline["tools"][name]
        current = {tool.name: _sha({"name": tool.name, "description": tool.description, "schema": tool.schema})
                   for tool in catalog}
        assert [tool.name for tool in catalog if tool.name in frozen_tools["each"]] == frozen_tools["order"], name
        assert {key: current.get(key) for key in frozen_tools["each"]} == frozen_tools["each"], name


def _sha(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
