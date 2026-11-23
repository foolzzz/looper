"""Frozen model-facing bytes from the reviewed prefix before the explorer service.

Hashes cover the existing prompts/contracts and the actual MCP listings, not
just whether a subagent tool's name happens to be absent.
"""

from __future__ import annotations

import hashlib
import importlib
import json
import pkgutil
from pathlib import Path


def _hash(value) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":")).encode()).hexdigest()


def _surfaces() -> dict[str, str]:
    from looper.agents.ops import Tool
    from looper.agents import ops
    from looper.agents.ops import planner, room, service, vault
    from looper.agents.room import MemberKind

    result = {}
    for name in ("tiers.manager", "tiers.planner", "tiers.worker", "tiers.planner_protocol", "system_prompt"):
        module = importlib.import_module(f"looper.agents.{name}")
        result[f"prompts:{name}"] = _hash({key: value for key, value in vars(module).items()
                                          if key.isupper() and isinstance(value, str)})
    all_tools = {}
    for info in pkgutil.iter_modules(ops.__path__):
        module = importlib.import_module(f"{ops.__name__}.{info.name}")
        for value in vars(module).values():
            for tool in value if isinstance(value, (tuple, list)) else (value,):
                if isinstance(tool, Tool):
                    all_tools[tool.name] = tool.listing()
    result["tools:all"] = _hash(all_tools)
    result["tools:manager"] = _hash([tool.listing() for tool in service.MANAGER_TOOLS])
    result["tools:planner"] = _hash([tool.listing() for tool in (*planner.TOOLS, *planner.ROLE_TOOLS)])
    result["tools:worker"] = _hash([tool.listing() for tool in (*room.tools_for(MemberKind.WORK), *vault.TOOLS)])
    return result


def test_existing_prompts_and_tool_listings_remain_byte_identical() -> None:
    baseline = json.loads((Path(__file__).parent / "fixtures" / "subagents_default_off.json").read_text())
    assert _surfaces() == baseline["surfaces"]
