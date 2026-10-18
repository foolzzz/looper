"""Pure profile, derived profile and Manager-tool contracts for prompt mode."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from typing import Any

import pytest

from looper.agents.agent_settings import Layer
from looper.agents.layer_effort import Before, Derived, _apply, convert
from looper.agents.profile_registry import BUILTIN_PROFILE
from looper.agents.runtimes import SystemPromptMode


@pytest.mark.parametrize("mode", list(SystemPromptMode))
@pytest.mark.parametrize("supported", [False, True])
def test_profile_replacement_requires_explicit_runtime_capability(
    monkeypatch: pytest.MonkeyPatch, mode: SystemPromptMode, supported: bool,
) -> None:
    from looper.agents import runtimes
    from looper.agents.profile_registry import RegistryError, check_system_prompt_mode
    from looper.agents.runtimes.claude_code import ClaudeCodeRuntime

    class StandIn(ClaudeCodeRuntime):
        type = "prompt-stand-in"
        label = "Prompt stand-in"
        can_replace_system_prompt = supported

    monkeypatch.setitem(runtimes.ADAPTERS, StandIn.type, StandIn())
    runtime = replace(BUILTIN_PROFILE.runtime, type=StandIn.type, name="My runtime")
    if mode is SystemPromptMode.REPLACE and not supported:
        with pytest.raises(RegistryError, match="My runtime.*the Prompt stand-in runtime cannot replace"):
            check_system_prompt_mode(runtime, mode)
    else:
        check_system_prompt_mode(runtime, mode)
    monkeypatch.delitem(runtimes.ADAPTERS, StandIn.type)
    if mode is SystemPromptMode.REPLACE:
        with pytest.raises(RegistryError, match="My runtime.*prompt-stand-in.*cannot replace"):
            check_system_prompt_mode(runtime, mode)
    else:
        check_system_prompt_mode(runtime, mode)


def test_derived_profiles_keep_system_prompt_mode_and_do_not_reuse_append_profiles() -> None:
    replacing = replace(BUILTIN_PROFILE, id="replacement", name="replacement", system_prompt_mode=SystemPromptMode.REPLACE)
    append_high = replace(BUILTIN_PROFILE, id="append-high", name="append-high", effort="high")
    key = (Layer.SYSTEM, None, None)
    before = {key: Before(replacing, "high")}
    conversion = convert(before, [replacing, append_high])
    assert isinstance(conversion.layers[key], Derived)
    inserted = []

    async def insert(fields: dict[str, Any], **_kwargs: Any) -> str:
        inserted.append(fields)
        return "derived"

    class Connection:
        async def execute(self, *_args: Any) -> None:
            pass

    asyncio.run(_apply(Connection(), before, conversion, insert))
    assert inserted[0]["system_prompt_mode"] == SystemPromptMode.REPLACE


def test_manager_profile_tool_exposes_and_returns_system_prompt_mode() -> None:
    from looper.agents.ops.config import TOOLS, _profile_listed

    tool = next(tool for tool in TOOLS if tool.name == "looper_agent_profiles")
    mode = tool.schema["properties"]["system_prompt_mode"]
    assert mode["enum"] == ["append", "replace"]
    assert "refused" in mode["description"]
    view = {"profile_id": "profile", "name": "profile", "runtime_name": "Claude Code", "provider_name": "login",
            "model": "sonnet", "effort": None, "agent_teams": False, "system_prompt_mode": "replace",
            "roles": [], "builtin": False, "used_by": {"layers": [], "task_count": 0, "schedule_count": 0,
                                                          "allowed_in": []}}
    assert _profile_listed(view)["system_prompt_mode"] == "replace"
