"""The private drafting role has its own profile and no executable tools or harness."""

from pathlib import Path
from dataclasses import replace

from tests.runtime_model_helpers import resolved_profile

from looper.agents.agent_settings import Access, AgentType, Launch, Layer, LayerValues, resolve
from looper.agents.spawn import ChildRun, SpawnKind
from looper.agents.system_prompt import SystemPrompt
from looper.agents.system_prompt_mode import SystemPromptMode
from looper.db.llm_records import RUN_KINDS


def test_drafting_role_has_an_isolated_builtin_profile_and_no_harness():
    role = AgentType("memory_draft")
    settings = resolve(role, {(Layer.SYSTEM, None): LayerValues(harness_prompt="PRIVATE HARNESS")})
    assert settings.profile.name == "claude-code/sonnet-5-5"
    from looper.agents.memory_draft_role import system_prompt

    assert "PRIVATE HARNESS" not in system_prompt(settings).text
    assert settings.harness.text not in system_prompt(settings).text
    assert settings.system_prompt_mode is SystemPromptMode.REPLACE
    profile = settings.profile
    settings = replace(settings, profile=resolved_profile(profile.id, profile.name, profile.runtime,
                                                         profile.provider, profile.model))
    launch = Launch(settings, role=role, access=Access.ADVISOR)
    command = launch.runtime.run_command("claude", cwd=Path("."), prompt_file=Path("prompt.md"), launch=launch)
    assert command[command.index("--tools") + 1] == ""
    assert "--system-prompt-file" in command
    assert "--plugin-dir" not in command


def test_drafting_spawn_uses_its_own_run_kind():
    child = ChildRun(SpawnKind("memory_draft"), SystemPrompt("contract", []), "input", 120,
                     role=AgentType("memory_draft"), private=True)
    assert child.record_kind == "memory_draft"
    assert child.agent_id.startswith("memory_draft-")
    assert child.record_kind in RUN_KINDS


def test_the_caller_can_lower_the_drafting_deadline(tmp_path):
    import asyncio
    from types import SimpleNamespace

    from looper.agents import memory_draft_role, model_call
    from looper.agents.spawn import SpawnParent

    class Backend:
        async def run(self, request):
            assert request.child.timeout_s == 2
            return model_call.Execution(None, failure=model_call.CallFailure.TIMEOUT, reason="deadline")

    done = asyncio.run(memory_draft_role.run(
        SimpleNamespace(prompt="synthetic input"), None, None, None, parent=SpawnParent(None),
        runtime_root=tmp_path, timeout_s=2, backend=Backend(),
        agent_settings=resolve(AgentType.MEMORY_DRAFT, {}), role_config={"timeout_seconds": 30}))
    assert isinstance(done, model_call.CallFailed) and done.failure is model_call.CallFailure.TIMEOUT
