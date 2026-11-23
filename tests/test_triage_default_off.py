"""Event triage leaves model-visible tools unchanged and has its own subscription kind."""

import hashlib
import json
from pathlib import Path

from looper.agents import subscriptions
from looper.agents.ops.clients import CLIENT_TOOLS
from looper.agents.ops.service import MANAGER_TOOLS

# Each tool's name, description and argument schema before the triage foundation, and the tools' order; a tool another
# feature adds since is not compared.
BASELINE = json.loads((Path(__file__).parent / "fixtures" / "triage_default_off_tools.json").read_text())


def _digest(tool) -> str:
    value = {"name": tool.name, "description": tool.description, "schema": tool.schema}
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def test_tools_are_byte_identical_to_before_the_triage_foundation():
    for kind, tools in (("manager", MANAGER_TOOLS), ("client", CLIENT_TOOLS)):
        frozen = BASELINE[kind]
        current = {tool.name: _digest(tool) for tool in tools}
        assert [tool.name for tool in tools if tool.name in frozen["each"]] == frozen["order"], kind
        assert {name: current.get(name) for name in frozen["each"]} == frozen["each"], kind


def test_triage_is_a_recognized_subscription_kind():
    assert subscriptions.parse({"output_kind": "triage"}, update=True)["output_kind"] == "triage"
