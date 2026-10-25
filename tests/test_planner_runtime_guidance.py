"""Planner inspection guidance travels with the selected runtime's read-only launch."""
from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from looper.agents.agent_settings import AgentSettings
from looper.agents.profile_registry import Endpoint, ModelSpec, ProviderSpec, ResolvedAgentProfile, ResolvedModel, RuntimeSpec
from looper.agents.runtimes import ApiProtocol, AuthKind, ReadOnly, VendorType, adapter
from looper.agents.step_workspaces import StepWorkspace
from looper.agents.tiers import planner as module


@pytest.fixture
def planner_launch(monkeypatch, tmp_path, agent_db):
    """Capture the real turn's command and prompt before it starts a model process."""
    async def capture(runtime, saved_prompt=None, workspace=False):
        runtime_adapter = adapter(runtime)
        protocol = (ApiProtocol.ANTHROPIC_MESSAGES if runtime == "claude-code"
                    else ApiProtocol.OPENAI_RESPONSES)
        provider = ProviderSpec("inspection-account", "Inspection", VendorType.CUSTOM, AuthKind.API_KEY,
                                api_key="test-only")
        model = ModelSpec("inspection-model", provider.id, "inspection-model", protocol)
        profile = ResolvedAgentProfile(
            "inspection-profile", "Inspection",
            RuntimeSpec("inspection-runtime", runtime, runtime, runtime, runtime_adapter.capabilities),
            model.id, resolved_model=ResolvedModel(
                model, provider, Endpoint(provider.id, protocol, "http://localhost.invalid/v1")))
        agent = module.PlannerAgent(task_id="inspection", runtime_root=tmp_path / "runtime")
        work = tmp_path / "project"
        work.mkdir(exist_ok=True)
        plan = module.TaskPlan("plan-inspection", "goal-inspection", "Inspect the result", str(work))
        agent._plan = plan
        if workspace:
            tree = tmp_path / "step-worktree"
            tree.mkdir()
            (tree / "result.txt").write_text("artifact ready\n")
            plan.steps.append(module.PlanStep(
                "step-inspection", 0, "Produce result.txt", status="deciding",
                workspace=StepWorkspace(1, "inspection", "step-inspection", str(work), "",
                                        "refs/heads/main", str(tree), "looper/inspection/step", "a" * 40)))
        monkeypatch.setattr(agent, "_load_agent_settings", AsyncMock(return_value=AgentSettings(profile=profile)))
        monkeypatch.setattr(module.db_role_configs, "get_config", AsyncMock(return_value=(
            None if saved_prompt is None else {"system_prompt": saved_prompt,
                                              "default_prompt": module.DEFAULT_SYSTEM_PROMPT})))
        captured = {}
        command_builder = agent._claude_command

        class Captured(Exception):
            pass

        def command(prompt_file, settings, launch, cwd):
            captured.update(prompt=Path(prompt_file).read_text(), launch=launch, plan=plan, agent=agent,
                            argv=command_builder(prompt_file, settings, launch, cwd))
            raise Captured

        monkeypatch.setattr(agent, "_claude_command", command)
        try:
            with pytest.raises(Captured):
                await agent._run_turn("[PLAN] Inspect before planning.")
        finally:
            await agent._claude.stop()
        return captured

    return capture


@pytest.mark.parametrize("runtime", ["claude-code", "codex"])
def test_selected_planner_runtime_has_usable_inspection_without_write_access(planner_launch, runtime):
    captured = asyncio.run(planner_launch(runtime))
    prompt, launch, argv = (captured[key] for key in ("prompt", "launch", "argv"))
    assert launch.runtime.type == runtime
    assert launch.read_only and not launch.agent_teams
    inspection = prompt.split("- You run read-only:", 1)[1].split("\n- Split", 1)[0]
    assert "Read, Glob and Grep" in inspection
    assert "read-only shell" in inspection
    assert "cat" in inspection and "rg" in inspection
    assert "leave commands" not in " ".join(inspection.lower().split())
    assert "build" in inspection and "test" in inspection and "Workers" in inspection
    if runtime == "codex":
        assert launch.read_only_mode is ReadOnly.SANDBOX
        assert 'sandbox_mode="read-only"' in argv and 'approval_policy="never"' in argv
        assert "--dangerously-bypass-approvals-and-sandbox" not in argv
    else:
        assert launch.read_only_mode is ReadOnly.PERMISSIONS
        denied = argv[argv.index("--disallowedTools") + 1].split(",")
        assert {"Bash", "Write", "Edit"} <= set(denied)
        assert "--dangerously-skip-permissions" not in argv


@pytest.mark.parametrize("runtime", ["claude-code", "codex"])
def test_step_artifact_is_addressable_with_the_same_read_only_boundary(planner_launch, runtime):
    captured = asyncio.run(planner_launch(runtime, workspace=True))
    launch, plan, agent, argv = (captured[key] for key in ("launch", "plan", "agent", "argv"))
    tree = Path(plan.steps[0].workspace.path)
    assert (tree / "result.txt").read_text() == "artifact ready\n"
    report = agent._work_report(plan.steps[0], {"status": "completed", "changed_files": {"result.txt": "added"}})
    assert str(tree) in "\n".join(report) and "result.txt" in "\n".join(report)
    assert launch.read_dirs == (str(tree),)
    assert "step's worktree" in captured["prompt"]
    if runtime == "claude-code":
        allowed = argv[argv.index("--allowedTools") + 1].split(",")
        assert f"Read(/{tree.as_posix()}/**)" in allowed
        assert not any(rule.startswith(("Bash", "Write", "Edit")) for rule in allowed)
    else:
        assert 'sandbox_mode="read-only"' in argv
        assert "--dangerously-bypass-approvals-and-sandbox" not in argv


@pytest.mark.parametrize("runtime", ["claude-code", "codex"])
def test_custom_saved_planner_prompt_stays_custom_and_read_only(planner_launch, runtime):
    custom = "Plan carefully. Ask a Worker to inspect every file; use no shell yourself."
    captured = asyncio.run(planner_launch(runtime, saved_prompt=custom))
    assert custom in captured["prompt"]
    assert module.DEFAULT_SYSTEM_PROMPT not in captured["prompt"]
    assert captured["launch"].read_only
    assert captured["launch"].read_only_mode in (ReadOnly.PERMISSIONS, ReadOnly.SANDBOX)
