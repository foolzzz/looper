"""Each existing surface is pinned to the reviewed revision, never a moving ref."""
from __future__ import annotations

import ast
import asyncio
import hashlib
import importlib
import json
import pkgutil
from pathlib import Path

import pytest

REVISION = "5f03d2929d14d21c6cc69ec053e0230370829f44"
FIXTURE = Path(__file__).parent / "fixtures" / "memory_lessons_off.json"
ROOT = Path(__file__).resolve().parents[1]
# Function bodies are separate freezes, guarding the real unchanged behavior as well as rendered constants.
FUNCTIONS = {
    "looper/agents/tiers/planner.py": ["PlannerAgent._plan_message", "PlannerAgent._resume_brief",
        "PlannerAgent._capture_project_learnings", "PlannerAgent._distill_lessons", "PlannerAgent._lessons_message"],
    "looper/agents/system_prompt.py": ["build_system_prompt", "build_role_prompt", "work_contract", "review_contract"],
    "looper/agents/claude_cli.py": ["ResidentClaude.command", "budget_args", "system_prompt_args", "permission_args", "library_args", "knowledge_args"],
    "looper/agents/runtimes/codex.py": ["CodexRuntime._command", "CodexRuntime.resident_command", "CodexRuntime.run_command"],
    "looper/agents/task_notice.py": ["notice_text"],
    "looper/agents/memory_routing.py": ["route"],
}


def _hash(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":")).encode()).hexdigest()


def recorded_surfaces(root=ROOT):
    from looper.agents import ops
    from looper.agents.ops import Tool
    from looper.agents.ops.config import _model_project
    from looper.agents.project_execution import ProjectExecution

    from looper.agents.agent_settings import Harness, Layer, AgentSettings, Launch
    from looper.agents import system_prompt
    from looper.agents.tiers.planner import PlannerAgent, TaskPlan
    agent = PlannerAgent(task_id="synthetic-task", runtime_root=Path("."))
    plan = TaskPlan("synthetic-project", "synthetic-goal", "Build a synthetic project", "")
    result = {"rendered:planner-plan": _hash(agent._plan_message(plan)),
              "rendered:planner-resume": _hash(agent._resume_brief(plan)),
              "rendered:planner-lessons": _hash(agent._lessons_message(plan)),
              "project:view": _hash(ProjectExecution().view()),
              "manager:project": _hash(_model_project({"id": "p", "name": "Synthetic", **ProjectExecution().view()}))}
    from looper.agents.profile_registry import ProviderSpec, ModelSpec, Endpoint, ResolvedModel, ResolvedAgentProfile, RuntimeSpec
    from looper.agents.runtimes import VendorType, ApiProtocol, adapter
    from looper.agents import claude_cli
    from looper.agents.library import AgentLibrary
    from looper.agents.knowledge import AgentKnowledge
    from looper.agents.task_notice import notice_text, NotifyChannel
    for channel in (NotifyChannel.TELEGRAM, NotifyChannel.LARK):
        result[f"rendered:notice:{channel.value}"] = _hash(notice_text("synthetic-task", "Synthetic", "completed",
            "Checked synthetic evidence.", channel=channel))
    result["argv:system-prompt"] = _hash(claude_cli.system_prompt_args(Path("prompt.md")))
    for runtime_name, vendor, protocol in (("codex", VendorType.CODEX_LOGIN, ApiProtocol.OPENAI_RESPONSES),
                                          ("claude-code", VendorType.CLAUDE_SUBSCRIPTION, ApiProtocol.ANTHROPIC_MESSAGES)):
        runtime = adapter(runtime_name)
        provider = ProviderSpec("synthetic-provider", "Synthetic", vendor, vendor.auth_kind, vendor.oauth_runtime_type)
        spec = ModelSpec("synthetic-model", provider.id, "synthetic-model", protocol)
        resolved = ResolvedModel(spec, provider, Endpoint(provider.id, protocol, ""))
        profile = ResolvedAgentProfile("synthetic-profile", "Synthetic", RuntimeSpec("rt", runtime_name, runtime_name,
            runtime.default_executable, runtime.capabilities), spec.id, "low", resolved_model=resolved)
        launch = Launch(AgentSettings(profile=profile), AgentLibrary(), AgentKnowledge())
        result[f"argv:{runtime_name}"] = _hash(runtime.launch_args(launch))
    for role, contract in (("worker", system_prompt.WORK_CONTRACT), ("reviewer", system_prompt.review_contract())):
        prompt = system_prompt.build_system_prompt(harness=Harness("Work carefully.", Layer.BUILTIN),
            role_id=role, role="Synthetic role", recommended=False, contract_name=role, contract=contract,
            project={"id":"p","name":"Synthetic"}, task="Complete the synthetic task.")
        result[f"rendered:{role}-system"] = _hash(prompt.text)
        result[f"rendered:{role}-contract"] = _hash(contract)
    for name in ("system_prompt", "tiers.planner", "tiers.worker"):
        module = importlib.import_module("looper.agents." + name)
        for key, value in vars(module).items():
            if key.isupper() and isinstance(value, str):
                result[f"constant:{name}.{key}"] = _hash(value)
    for info in pkgutil.iter_modules(ops.__path__):
        module = importlib.import_module(f"{ops.__name__}.{info.name}")
        for value in vars(module).values():
            for tool in value if isinstance(value, (list, tuple)) else (value,):
                if isinstance(tool, Tool):
                    result[f"tool:{tool.name}"] = _hash(tool.listing())
    for file, names in FUNCTIONS.items():
        source = (root / file).read_text()
        tree = ast.parse(source)
        found = {}
        def visit(nodes, prefix=""):
            for node in nodes:
                if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                    name = prefix + node.name
                    found[name] = ast.get_source_segment(source, node)
                    if isinstance(node, ast.ClassDef):
                        visit(node.body, name + ".")
        visit(tree.body)
        for name in names:
            assert name in found, (file, name)
            result[f"function:{file}:{name}"] = _hash(found[name])
    return result


@pytest.mark.parametrize("global_mode,project_mode", [(None,None),(False,"on"),(True,None),(True,"off")])
def test_disabled_lessons_preserve_each_recorded_surface(monkeypatch, global_mode, project_mode):
    from looper.agents.project_execution import ProjectExecution
    from looper.agents.ops.config import _model_project
    baseline = json.loads(FIXTURE.read_text())
    assert baseline["revision"] == REVISION
    current = recorded_surfaces()
    for key, expected in baseline["surfaces"].items():
        assert current[key] == expected, key
    from looper.agents import memory_lessons
    from looper.db import system_settings
    async def settings(keys, **kwargs):
        return {} if global_mode is None else {"memory_lessons":json.dumps({"enabled":global_mode})}
    monkeypatch.setattr(system_settings,"get_settings",settings)
    assert asyncio.run(memory_lessons.settings_view())["enabled"] is (global_mode is True)
    execution = ProjectExecution.from_row({"memory_lessons": project_mode})
    if project_mode != "on":
        assert _hash(execution.view()) == baseline["surfaces"]["project:view"]
    assert _hash(_model_project({"id":"p", "name":"Synthetic", **execution.view()})) == baseline["surfaces"]["manager:project"]
    assert execution.lessons_on is False


def test_settings_alone_start_no_model_or_delivery(monkeypatch, tmp_path):
    from datetime import datetime, timezone
    from looper.agents import housekeeping, memory_routing, memory_settings
    from looper.agents.message import AgentAddress, MessageEnvelope, MessageType
    from tests.db_support import run_db, v1_api
    async def forbidden(*args, **kwargs):
        pytest.fail("lesson settings started a model, channel or vault action")
    from looper.agents import spawn, vault
    monkeypatch.setattr(spawn, "run_child", forbidden)
    monkeypatch.setattr(vault, "update_settings", forbidden)
    from looper.agents.channels.base import ChannelAdapter
    monkeypatch.setattr(ChannelAdapter, "notify", forbidden)
    async def check(pool):
        async with v1_api(tmp_path,pool) as call:
            status, asked = await call("memory_lessons.settings_put", {"enabled":True,"auto_activate":True})
            assert status == 202, asked
            status, answer = await call("confirm", {"confirmation_id":asked["confirmation_id"]})
            assert status == 200 and answer["result"]["enabled"], answer
            assert (await call("memory_lessons.settings_get"))[0] == 200
        await housekeeping.run_once(tmp_path, datetime.now(timezone.utc), current_runner=None, commands=None)
        # With the existing vault-memory route enabled too, lesson commands remain unclaimed.
        async def stored():
            return {"enabled":True,"push_channel":"synthetic"}
        monkeypatch.setattr(memory_settings,"stored",stored)
        for text in ("撤销 M-1", "续期 M-1", "生效 M-1", "提议 M-1"):
            envelope = MessageEnvelope("m","c","2026-11-23T00:00:00Z",AgentAddress("channel","synthetic","telegram"),
                AgentAddress("manager","m"),MessageType.USER_MESSAGE,{"text":text})
            assert await memory_routing.route(envelope) is None
        for table in ("memory_retro_runs","memory_injections","memory_weekly_cards","task_lesson_snapshots"):
            assert await pool.fetchval(f"SELECT count(*) FROM {table}") == 0
    run_db(check, monkeypatch, timeout=60)
