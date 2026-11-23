"""Frozen model-facing bytes from the reviewed prefix before the explorer service.

Hashes cover the existing prompts/contracts and the actual MCP listings, not
just whether a subagent tool's name happens to be absent. Each item is frozen
on its own (a prompt constant, a tool's listing, a role's existing tools in
order), so another feature's added constant or tool changes none of them.
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


def _surfaces(baseline: dict) -> dict:
    """The current bytes of every item ``baseline`` froze: each prompt constant, each tool's listing, and the order of
    each role's existing tools. An item added since (another feature's constant or tool) is not part of it."""
    from looper.agents.ops import Tool
    from looper.agents import ops
    from looper.agents.ops import planner, room, service, vault
    from looper.agents.room import MemberKind

    prompts = {}
    for name, constants in baseline["prompts"].items():
        module = importlib.import_module(f"looper.agents.{name}")
        current = {key: _hash(value) for key, value in vars(module).items() if key.isupper() and isinstance(value, str)}
        prompts[name] = {key: current.get(key) for key in constants}
    all_tools = {}
    for info in pkgutil.iter_modules(ops.__path__):
        module = importlib.import_module(f"{ops.__name__}.{info.name}")
        for value in vars(module).values():
            for tool in value if isinstance(value, (tuple, list)) else (value,):
                if isinstance(tool, Tool):
                    all_tools[tool.name] = tool.listing()
    each = {name: _hash(all_tools[name]) if name in all_tools else None for name in baseline["tools:each"]}
    order = {}
    # Explorer tools must never appear on a disabled run's MCP listing.
    from looper.agents.ops.subagents import TOOL_NAMES
    for name, tools in {
        "manager": service.MANAGER_TOOLS,
        "planner": (*planner.TOOLS, *planner.ROLE_TOOLS),
        "worker": (*room.tools_for(MemberKind.WORK), *vault.TOOLS),
    }.items():
        assert TOOL_NAMES.isdisjoint(tool.name for tool in tools), name
        frozen = set(baseline["tools:order"][name])
        assert frozen <= baseline["tools:each"].keys(), name  # each listed tool's bytes are frozen in tools:each
        order[name] = [tool.name for tool in tools if tool.name in frozen]
    return {"prompts": prompts, "tools:each": each, "tools:order": order}


def test_existing_prompts_and_tool_listings_remain_byte_identical() -> None:
    baseline = json.loads((Path(__file__).parent / "fixtures" / "subagents_default_off.json").read_text())
    assert _surfaces(baseline["surfaces"]) == baseline["surfaces"]


def test_disabled_run_grant_lists_exactly_the_existing_worker_tools() -> None:
    from looper.agents.ops import room
    from looper.agents.room import MemberKind, RoomService, Seat
    ops = room.RoomOps(RoomService(), url="http://localhost:1")
    token = ops.issue("task", Seat("worker@s1", "worker", MemberKind.WORK, run="worker-1"))
    assert ops.listing("Bearer " + token) == [tool.listing() for tool in room.tools_for(MemberKind.WORK)]
    assert "subagents" not in ops.export_run("worker-1")
