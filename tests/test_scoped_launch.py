"""Pure capability mapping and defense-in-depth launch checks."""
from types import SimpleNamespace

import pytest

from looper.agents import chat_scopes as scopes
from looper.agents.agent_settings import Access
from looper.agents.library import AgentLibrary, ItemKind, LoadedItem


@pytest.mark.parametrize("name,arguments,capability", [
    ("looper_tasks", {}, "tasks.read"),
    ("looper_task_create", {}, "tasks.create"),
    ("looper_task_gate_reply", {}, "tasks.instruct"),
    ("looper_task_control", {"action": "restart"}, "tasks.restart"),
    ("looper_task_control", {"action": "cancel"}, "full"),
    ("looper_task_control", {"action": ["start"]}, "full"),
    ("looper_schedules", {"action": "get"}, "schedules.read"),
    ("looper_schedules", {"action": "create"}, "schedules.manage"),
    ("looper_confirm", {}, "full"),
    ("looper_task_profiles", {}, "full"),
    ("synthetic_unknown_tool", {}, "unknown"),
])
def test_capability_of(name, arguments, capability):
    assert scopes.capability_of(name, arguments) == capability


@pytest.mark.parametrize("access,items,command", [
    (Access.PLATFORM_TOOLS, (), ["claude", "--dangerously-skip-permissions"]),
    (Access.PLATFORM_TOOLS, (), ["claude", "--permission-mode", "bypassPermissions"]),
    (Access.FULL, (), []),
    (Access.PLATFORM_TOOLS, (LoadedItem(ItemKind.SKILL, "synthetic", "sha"),), []),
    (Access.PLATFORM_TOOLS, (LoadedItem(ItemKind.MCP, "synthetic", "sha"),), []),
])
def test_check_launch_refuses_widening(access, items, command):
    scope = SimpleNamespace(mode=scopes.LaunchMode.PLATFORM_TOOLS)
    launch = SimpleNamespace(access_level=access, library=AgentLibrary(items=items))
    with pytest.raises(scopes.ScopeInvariantViolation):
        scopes.check_launch(scope, launch, command=command)


def test_check_launch_allows_only_builtin_server_and_preserves_full():
    launch = SimpleNamespace(access_level=Access.PLATFORM_TOOLS, library=AgentLibrary(
        items=(LoadedItem(ItemKind.MCP, "looper", "sha", scope="builtin"),)))
    scopes.check_launch(SimpleNamespace(mode=scopes.LaunchMode.PLATFORM_TOOLS), launch, command=[])
    launch.access_level = Access.FULL
    scopes.check_launch(SimpleNamespace(mode=scopes.LaunchMode.FULL), launch,
                        command=["--dangerously-skip-permissions"])
    scopes.check_launch(None, launch, command=["--dangerously-skip-permissions"])
