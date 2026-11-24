"""The retrospective has a private no-tool call and no unmeasured model fallback."""

import asyncio
from dataclasses import replace

import pytest

from looper.agents import memory_retro, memory_retro_role, model_call
from looper.agents.agent_settings import Access, AgentType, Launch, Layer, LayerValues, resolve
from looper.agents.spawn import SpawnKind, SpawnParent
from looper.agents.system_prompt_mode import SystemPromptMode
from tests.runtime_model_helpers import resolved_profile


def test_private_role_has_no_tools_harness_or_retained_text(tmp_path):
    settings = resolve(AgentType.MEMORY_RETRO, {(Layer.SYSTEM, None): LayerValues(harness_prompt="PRIVATE")})
    profile = settings.profile
    settings = replace(settings, profile=resolved_profile(profile.id, profile.name, profile.runtime,
        profile.provider, profile.model), profile_source=Layer.ROLE)
    assert settings.system_prompt_mode is SystemPromptMode.REPLACE
    assert "PRIVATE" not in memory_retro_role.system_prompt(settings).text
    launch = Launch(settings, role=AgentType.MEMORY_RETRO, access=Access.ADVISOR)
    command = launch.runtime.run_command("claude", cwd=tmp_path, prompt_file=tmp_path / "prompt", launch=launch)
    assert command[command.index("--tools") + 1] == "" and "--plugin-dir" not in command
    value = memory_retro.RetroInput("task", "project", "synthetic journal", {"J1": 1}, {})

    class Backend:
        async def run(self, request):
            assert isinstance(request, model_call.ChildCall)
            assert request.child.kind is SpawnKind.MEMORY_RETRO
            assert request.child.private and request.child.max_turns == 1 and request.child.timeout_s == 90
            return model_call.Execution('{"lessons":[],"note":"none"}', cost_usd=.01)

    done = asyncio.run(memory_retro_role.run(value, parent=SpawnParent("task"), runtime_root=tmp_path,
        agent_settings=settings, role_config={"timeout_seconds": 500}, backend=Backend()))
    assert isinstance(done, model_call.CallOk)
    for config, selected in (({"enabled": False}, settings), ({}, resolve(AgentType.MEMORY_RETRO, {}))):
        done = asyncio.run(memory_retro_role.run(value, parent=SpawnParent("task"), runtime_root=tmp_path,
            agent_settings=selected, role_config=config, backend=Backend()))
        assert isinstance(done, model_call.CallFailed) and done.failure is model_call.CallFailure.UNAVAILABLE
        assert done.cost_usd == 0


def test_retro_profile_rejects_a_runtime_without_no_tools():
    from looper.agents.profile_registry import RegistryError, RuntimeSpec, check_roles
    from looper.agents.runtimes import adapter

    profile = resolve(AgentType.MEMORY_RETRO, {}).profile
    runtime = adapter("codex")
    selected = replace(profile, runtime=RuntimeSpec("synthetic-codex", "Synthetic Codex", "codex",
                                                    capabilities=runtime.capabilities))
    with pytest.raises(RegistryError, match="lacks no_tools"):
        check_roles(selected, ["memory_retro"])
