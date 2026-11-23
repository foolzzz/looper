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


def _surfaces(baseline_tools: dict[str, str]) -> dict[str, str | dict[str, str]]:
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
    # Freeze existing tools individually so unrelated catalog additions are allowed.
    assert baseline_tools.keys() <= all_tools.keys(), "baseline tool missing from catalog"
    result["tools:each"] = {name: _hash(all_tools[name]) for name in baseline_tools}
    # Explorer tools must never appear on a disabled run's MCP listing.
    from looper.agents.ops.subagents import TOOL_NAMES
    for name, tools in {
        "manager": service.MANAGER_TOOLS,
        "planner": (*planner.TOOLS, *planner.ROLE_TOOLS),
        "worker": (*room.tools_for(MemberKind.WORK), *vault.TOOLS),
    }.items():
        assert TOOL_NAMES.isdisjoint(tool.name for tool in tools), name
        result[f"tools:{name}"] = _hash([tool.listing() for tool in tools])
    return result


def test_existing_prompts_and_tool_listings_remain_byte_identical() -> None:
    baseline = json.loads((Path(__file__).parent / "fixtures" / "subagents_default_off.json").read_text())
    assert _surfaces(baseline["surfaces"]["tools:each"]) == baseline["surfaces"]


def test_disabled_run_grant_lists_exactly_the_existing_worker_tools() -> None:
    from looper.agents.ops import room
    from looper.agents.room import MemberKind, RoomService, Seat
    ops = room.RoomOps(RoomService(), url="http://localhost:1")
    token = ops.issue("task", Seat("worker@s1", "worker", MemberKind.WORK, run="worker-1"))
    assert ops.listing("Bearer " + token) == [tool.listing() for tool in room.tools_for(MemberKind.WORK)]
    assert "subagents" not in ops.export_run("worker-1")


def test_planner_guide_is_byte_identical_unless_subagents_are_on_and_wired() -> None:
    from looper.agents.ops.planner import PlannerOps
    from looper.agents.ops.subagents import PLANNER_SUBAGENTS_GUIDE

    ops = PlannerOps(object(), url="http://localhost:1")
    for roles in (False, True):
        for room in (False, True):
            for vault in ("", "VAULT GUIDE"):
                plain = ops.guide_for(roles=roles, room=room, vault=vault)
                # The switch off, or the tools not wired: the same bytes as before the feature.
                assert ops.guide_for(roles=roles, room=room, vault=vault, subagents=False) == plain
                assert ops.guide_for(roles=roles, room=room, vault=vault, subagents=True) == plain
    ops.subagents = object()
    base = ops.guide_for(roles=True, room=False)
    assert ops.guide_for(roles=True, room=False, subagents=False) == base
    assert ops.guide_for(roles=True, room=False, subagents=True) == f"{base}\n{PLANNER_SUBAGENTS_GUIDE}"
    assert ops.guide_for(roles=False, vault="V", subagents=True).endswith(f"{PLANNER_SUBAGENTS_GUIDE}\nV")


def test_project_execution_keeps_the_derived_switch_out_of_its_view() -> None:
    from looper.agents.project_execution import ProjectExecution

    assert ProjectExecution.from_row(None) == ProjectExecution()
    baseline = ProjectExecution().view()
    for value in (None, "off", ""):
        execution = ProjectExecution.from_row({"subagents": value})
        assert not execution.subagents_on
        assert execution.view() == baseline | ({"subagents": "off"} if value == "off" else {})
    on = ProjectExecution.from_row({"subagents": "on"})
    # Project settings expose an explicit switch; the derived Planner flag stays private.
    assert on.subagents_on and on.view() == baseline | {"subagents": "on"}
    assert "subagents_on" not in on.view()


def test_planner_turn_text_is_untouched_when_off_or_nothing_finished() -> None:
    import asyncio

    from looper.agents.subagent_delivery import mark_turn_delivered, planner_turn_note

    # ``on=False`` never reads the database, so a turn of a project with the switch off adds nothing.
    assert asyncio.run(planner_turn_note("task", on=False)) == ("", [])
    assert asyncio.run(mark_turn_delivered([])) is None


def test_planner_system_prompt_with_the_switch_off_matches_the_recorded_base_bytes() -> None:
    import hashlib
    from dataclasses import replace

    from looper.agents.agent_settings import Harness, Layer
    from looper.agents.knowledge import EMPTY_KNOWLEDGE
    from looper.agents.ops.planner import PlannerOps
    from looper.agents.tiers.planner import PlannerAgent, PlannerSettings, TaskPlan

    recorded = json.loads((Path(__file__).parent / "fixtures" / "planner_prompt_subagents_off.json").read_text())
    agent = PlannerAgent(task_id="task-baseline", runtime_root=Path("."))
    agent._ops = PlannerOps(object(), url="http://localhost:1")
    agent._ops.subagents = object()  # wired, as in `looper serve`

    def prompt() -> str:
        return agent._system_prompt(
            TaskPlan("p", "g", "Build the project", ""), PlannerSettings("Plan carefully.", 60, 0, 0),
            EMPTY_KNOWLEDGE, Harness("Work carefully.", Layer.BUILTIN)).text

    off = prompt()
    # Passing subagents=True regardless of the project would change these bytes.
    assert hashlib.sha256(off.encode()).hexdigest() == recorded["sha256"]
    agent._execution = replace(agent._execution, subagents_on=True)
    assert prompt() != off
