"""Intent replies add no model-facing prompt, tool or launch-signature content.

These hashes pin the main baseline before intent routing was added. The routing
suite separately exercises the absent-setting branch through a real Manager turn.
"""

import ast
import hashlib
import inspect
import json
import textwrap

from looper.agents import agent_settings
from looper.agents.ops import clients, planner, room, service
from looper.agents.tiers import manager, planner as planner_tier, worker


def _digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def test_default_prompts_and_tool_list_equal_the_main_baseline():
    prompts = [manager.DEFAULT_SYSTEM_PROMPT, planner_tier.DEFAULT_SYSTEM_PROMPT,
               worker.DEFAULT_SYSTEM_PROMPT, agent_settings.DEFAULT_HARNESS_PROMPT]
    tools = [(tool.name, tool.description, tool.schema) for tool in (
        *service.MANAGER_TOOLS, *clients.CLIENT_TOOLS, *planner.TOOLS,
        *planner.ROLE_TOOLS, *room.TOOLS,
    )]
    assert _digest(prompts) == "113dcfc6ecb667fd2d45c4ba855505e86ded3941ffafa669091bed02b221ff28"
    assert _digest(tools) == "76a6006374afaae65cb1525742bf74c3c99dc0a7954e441d51844e7668655346"


def test_the_manager_launch_command_and_signature_equal_the_main_baseline():
    command = ast.parse(textwrap.dedent(inspect.getsource(manager.ManagerAgent._claude_command)))
    preparation = ast.parse(textwrap.dedent(inspect.getsource(manager.ManagerAgent._prepare_turn)))
    signature = []
    for node in ast.walk(preparation):
        if not isinstance(node, (ast.Assign, ast.AugAssign)):
            continue
        targets = [node.target] if isinstance(node, ast.AugAssign) else node.targets
        if any(isinstance(part, ast.Name) and part.id == "signature"
               for target in targets for part in ast.walk(target)):
            signature.append(ast.dump(node, include_attributes=False))
    assert _digest(ast.dump(command, include_attributes=False)) == (
        "93e8547693d876bcff1e6f898216cd844829f11eba29ebe6feab1f10098771d3"
    )
    assert _digest(signature) == "b5f239ff88634e134bb4a465b9fc635852d4ddc60953c2fb11494d66ba453a76"
