"""Per-item output freezes from the fixed pre-retrospective revision."""
from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

REVISION = "ad62143e7fc02afa840cd6771a730836d72fed89"
FOUNDATION_REVISION = "5f03d2929d14d21c6cc69ec053e0230370829f44"
FIXTURE = Path(__file__).parent / "fixtures" / "memory_retro_off.json"


def _hash(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":")).encode()).hexdigest()


async def surfaces(project_mode=None):
    """Render real ordinary prompts, commands, tool schemas and routing results."""
    from looper.agents import claude_cli, memory_routing, system_prompt
    from looper.agents.agent_settings import AgentSettings, AgentType, Boundaries, Harness, Layer, Launch
    from looper.agents.knowledge import AgentKnowledge
    from looper.agents.library import AgentLibrary
    from looper.agents.message import AgentAddress, MessageEnvelope, MessageType
    from looper.agents.ops import memory_propose
    from looper.agents.ops.config import _model_project
    from looper.agents.profile_registry import (
        Endpoint, ModelSpec, ProviderSpec, ResolvedAgentProfile, ResolvedModel, RuntimeSpec,
    )
    from looper.agents.project_execution import ProjectExecution
    from looper.agents.runtimes import ApiProtocol, VendorType, adapter
    from looper.agents.task_notice import NotifyChannel, notice_text
    from looper.agents.task_roles import RoleKind, TaskRole
    from looper.agents.tiers.journal import JournalEntry, JournalKind
    from looper.agents.tiers.planner import PlannerAgent, TaskPlan
    from looper.agents.vault import VaultConfig

    entry = JournalEntry(step_id="step-synthetic", step="s1", kind=JournalKind.STEP_ACCEPTED,
                         summary="Checked the parser", learnings=[])
    plan = TaskPlan("plan-synthetic", "goal-synthetic", "Check the parser", "fixture-work", journal=[entry])
    agent = PlannerAgent(task_id="task-synthetic", runtime_root=Path("fixture-runtime"))
    values = {
        "planner:PLAN": agent._plan_message(plan),
        "planner:RESUME": agent._resume_brief(plan),
        "planner:LESSONS": agent._lessons_message(plan),
        "planner:VAULT": agent._vault_message(plan, "completed", VaultConfig(Path("notes"), ("knowledge",), None), None),
        "project:execution": ProjectExecution().view(),
        "project:manager": _model_project({"id": "project-synthetic", "name": "Synthetic",
                                            **ProjectExecution().view()}),
        "manager:memory-tool": memory_propose.TOOL.listing(),
        "argv:system-prompt": claude_cli.system_prompt_args(Path("prompt.md")),
    }
    from looper.agents import knowledge
    from looper.agents.tiers.planner import PlanStep
    draft = AsyncMock(return_value={})
    agent._execution = ProjectExecution.from_row({"distill_lessons": "draft", "memory_lessons": project_mode})
    agent._activity = AsyncMock()
    plan.project_id = "project-synthetic"
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(knowledge, "add_draft", draft)
        await agent._capture_project_learnings(plan, PlanStep("step-synthetic", 0, "Check the parser", key="s1"),
            [{"text": "Run the parser check", "scope": "project"}], "Worker")
    values["capture:project-learning"] = {
        "drafts": [call.kwargs for call in draft.await_args_list],
        "activity": [list(call.args) for call in agent._activity.await_args_list],
    }
    harness = Harness("Work carefully.", Layer.BUILTIN)
    for role, contract, kind in (("worker", system_prompt.WORK_CONTRACT, RoleKind.WORK),
                                  ("reviewer", system_prompt.review_contract(), RoleKind.REVIEW)):
        values[f"{role}:contract"] = contract
        values[f"{role}:builtin"] = system_prompt.build_system_prompt(
            harness=harness, role_id=role, role="Synthetic role", recommended=False,
            contract_name=role, contract=contract, project={"id": "project-synthetic", "name": "Synthetic"},
            task="Check the parser.").text
        values[f"{role}:task-role"] = system_prompt.build_role_prompt(
            harness=harness, boundaries=Boundaries("Keep the public API."),
            task_role=TaskRole("synthetic-role", kind, 1, "Check synthetic evidence."),
            contract_name=role, contract=contract, project={"id": "project-synthetic", "name": "Synthetic"},
            task="Check the parser.").text
    for runtime_name, vendor, protocol in (("codex", VendorType.CODEX_LOGIN, ApiProtocol.OPENAI_RESPONSES),
                                           ("claude-code", VendorType.CLAUDE_SUBSCRIPTION,
                                            ApiProtocol.ANTHROPIC_MESSAGES)):
        runtime = adapter(runtime_name)
        provider = ProviderSpec("synthetic-provider", "Synthetic", vendor, vendor.auth_kind, vendor.oauth_runtime_type)
        model = ModelSpec("synthetic-model", provider.id, "synthetic-model", protocol)
        resolved = ResolvedModel(model, provider, Endpoint(provider.id, protocol, ""))
        profile = ResolvedAgentProfile("synthetic-profile", "Synthetic",
            RuntimeSpec("synthetic-runtime", runtime_name, runtime_name, runtime.default_executable, runtime.capabilities),
            model.id, "low", resolved_model=resolved)
        for role in (AgentType.PLANNER, AgentType.WORKER, AgentType.REVIEWER):
            launch = Launch(AgentSettings(profile=profile), AgentLibrary(), AgentKnowledge(), role)
            values[f"argv:{runtime_name}:{role.value}"] = runtime.launch_args(
                launch, **({"cwd": Path("/synthetic/project")} if runtime_name == "claude-code" else {}))
            values[f"permission:{runtime_name}:{role.value}"] = {
                "access": launch.access_level.value, "no_tools": launch.no_tools,
                "read_only": launch.read_only, "agent_teams": launch.agent_teams,
            }
    for channel in (NotifyChannel.LARK, NotifyChannel.TELEGRAM):
        for status in ("completed", "failed"):
            values[f"notice:{channel.value}:{status}"] = notice_text(
                "task-synthetic", "Check the parser", status, "Synthetic result.", channel=channel)
    for text in ("撤销 M-1", "续期 M-1", "生效 M-1", "提议 M-1"):
        envelope = MessageEnvelope("message-synthetic", "conversation-synthetic", "2026-11-23T00:00:00Z",
            AgentAddress("channel", "synthetic", "telegram"), AgentAddress("manager", "m"),
            MessageType.USER_MESSAGE, {"text": text})
        values[f"route:{text}"] = await memory_routing.route(envelope)
    return values


@pytest.mark.parametrize("raw,project_mode", [
    (None, "on"), ({"enabled": False}, "on"), ({"enabled": True}, "off"),
    ({"enabled": True}, None), ("invalid storage", "on"),
])
def test_disabled_retro_preserves_each_surface(monkeypatch, raw, project_mode):
    from looper.agents import memory_lessons, memory_settings, spawn, vault
    from looper.agents.channels.base import ChannelAdapter
    from looper.agents.project_execution import ProjectExecution
    from looper.agents.tiers.planner import PlannerAgent
    from looper.db import system_settings

    async def settings(keys, **kwargs):
        return {} if raw is None else {"memory_lessons": raw if isinstance(raw, str) else json.dumps(raw)}
    monkeypatch.setattr(system_settings, "get_settings", settings)
    # The old memory route may be enabled independently; lesson commands still fall through.
    monkeypatch.setattr(memory_settings, "stored", AsyncMock(return_value={"enabled": True, "push_channel": "synthetic"}))
    forbidden = AsyncMock(side_effect=AssertionError("disabled retrospective caused a side effect"))
    monkeypatch.setattr(spawn, "run_child", forbidden)
    monkeypatch.setattr(vault, "update_settings", forbidden)
    monkeypatch.setattr(ChannelAdapter, "notify", forbidden)
    baseline = json.loads(FIXTURE.read_text())
    current = asyncio.run(surfaces(project_mode))
    assert current.keys() == baseline.keys()
    for key, value in current.items():
        assert baseline[key]["source_sha"] == (FOUNDATION_REVISION if key == "capture:project-learning" else REVISION), key
        assert _hash(value) == baseline[key]["sha256"], key
    agent = PlannerAgent(task_id="task-synthetic", runtime_root=Path("fixture-runtime"))
    agent._execution = ProjectExecution.from_row({"memory_lessons": project_mode})
    assert asyncio.run(agent._project_lessons_enabled()) is False
    assert not asyncio.run(memory_lessons.settings_view())["problem"] or raw == "invalid storage"
    forbidden.assert_not_awaited()


def test_disabled_retro_keeps_independent_vault_closeout(monkeypatch, tmp_path):
    from looper.agents import spawn, untrusted, vault
    from looper.agents.project_execution import ProjectExecution
    from looper.agents.tiers.journal import JournalEntry, JournalKind
    from looper.agents.tiers.planner import PlannerAgent, TaskPlan, TurnKind

    async def check():
        forbidden = AsyncMock(side_effect=AssertionError("vault closeout started a retrospective"))
        monkeypatch.setattr(spawn, "run_child", forbidden)
        monkeypatch.setattr(vault, "active", AsyncMock(return_value=vault.VaultConfig(Path("notes"), ("knowledge",), None)))
        monkeypatch.setattr(vault, "rules_text", AsyncMock(return_value=(None, None)))
        monkeypatch.setattr(untrusted, "enabled", AsyncMock(return_value=False))
        agent = PlannerAgent(task_id="task-synthetic", runtime_root=tmp_path, ops=object())
        agent._token = "synthetic-token"
        agent._read_execution = AsyncMock(return_value=ProjectExecution.from_row({"vault": "on"}))
        agent._ask = AsyncMock(return_value="Nothing lasting.")
        agent._activity = AsyncMock()
        plan = TaskPlan("plan-synthetic", "goal-synthetic", "Check the parser", "fixture-work", journal=[
            JournalEntry(step_id="step-synthetic", step="s1", kind=JournalKind.STEP_ACCEPTED,
                         summary="Checked the parser", learnings=[])])
        await agent._vault_closeout(plan, "completed")
        call = agent._ask.await_args
        assert call.args[0] is TurnKind.VAULT
        assert _hash(call.args[1]) == json.loads(FIXTURE.read_text())["planner:VAULT"]["sha256"]
        agent._activity.assert_awaited_once_with("phase", "Vault closeout: Nothing lasting.")
        forbidden.assert_not_awaited()
    asyncio.run(check())
