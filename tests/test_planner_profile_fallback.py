"""Planner profile failures recover without changing step attempts or default prompts."""

import asyncio
import json
from dataclasses import replace
from unittest.mock import AsyncMock

import pytest

from looper.agents.agent_settings import AgentSettings
from looper.agents.message import MessageType
from looper.agents.profile_registry import builtin_resolved_profile
from looper.agents.tiers import planner as module
from tests.test_planner_agent import WORK_OK, REVIEW_PASS, decide, drive, plan, step


def test_worker_and_reviewer_fallbacks_are_journaled(planner_claude, agent_db):
    changes = [{"from": "primary", "to": "backup", "reason": "profile_error"}]
    claude = planner_claude(plan(step("s1", review=True)), decide("review"), decide("accept"))
    drive(claude, lambda envelope: {**(REVIEW_PASS if envelope.message_type is MessageType.VERIFY_REQUEST else WORK_OK),
                                    "fallback": changes})
    entries = [row for row in agent_db.store.journal["t1"] if row["kind"] == "profile_fallback"]
    assert [json.loads(row["summary"]) for row in entries] == [
        {"role": role, "step": "s1", **changes[0]} for role in ("worker", "reviewer")]


@pytest.mark.parametrize("has_fallback", [True, False])
def test_planner_profile_error_uses_only_configured_fallback(agent_db, monkeypatch, tmp_path, has_fallback):
    async def run():
        agent = module.PlannerAgent(task_id="fallback", runtime_root=tmp_path)
        agent._plan = module.TaskPlan("plan", "goal", "Inspect", str(tmp_path))
        original = AgentSettings(profile=replace(builtin_resolved_profile(), id="primary"), profile_error="unavailable")
        backup = AgentSettings(profile=replace(builtin_resolved_profile(), id="backup"))
        monkeypatch.setattr(agent, "_load_agent_settings", AsyncMock(return_value=original))
        monkeypatch.setattr(module, "fallback_settings", AsyncMock(return_value=backup if has_fallback else None))
        # Stop immediately after resolving a launch; no CLI or session state is needed.
        attached = []
        async def attach(launch, **kwargs):
            attached.append(launch)
            raise RuntimeError("launch reached")
        monkeypatch.setattr(module.llmrec, "attach", attach)
        with pytest.raises(RuntimeError if has_fallback else module.PlannerTurnError,
                           match="launch reached" if has_fallback else "unavailable"):
            await agent._turn("Inspect")
        assert [launch.profile.id for launch in attached] == (["backup"] if has_fallback else [])
        assert [entry.kind.value for entry in agent._plan.journal] == (["profile_fallback"] if has_fallback else [])
    asyncio.run(run())


def test_run_recorder_retains_reported_model_separately(agent_db, tmp_path):
    from looper.agents.agent_settings import Launch
    from looper.agents.claude_cli import ClaudeRunRecorder
    async def run():
        recorder = ClaudeRunRecorder(agent_id="worker-dev", task_id="task", command=[], cwd=str(tmp_path),
                                    input_text="Inspect", launch=Launch(AgentSettings(profile=builtin_resolved_profile())))
        async with recorder:
            await recorder.record({"type": "system", "subtype": "init", "model": "reported-model"})
            await recorder.finish(output_text="done", result_event={"type": "result", "subtype": "success"})
        assert agent_db.finish_record.call_args.kwargs["metadata"]["model_reported"] == "reported-model"
    asyncio.run(run())


def test_invalid_fallback_binding_retains_its_next_target(agent_db, monkeypatch):
    from looper.agents.agent_settings import AgentType, fallback_settings
    from looper.db import profile_registry

    initial = AgentSettings(profile=replace(builtin_resolved_profile(), fallback_profile_id="review_a"))
    row = {"id": "review_a", "name": "review_a", "runtime_id": "rt-claude-code",
           "runtime_name": "Claude Code", "runtime_type": "claude-code", "model_id": "missing-model",
           "fallback_profile_id": "review_b"}
    monkeypatch.setattr(profile_registry, "get_profile", AsyncMock(return_value=row))
    recovered = asyncio.run(fallback_settings(initial, AgentType.WORKER))
    assert recovered.profile.id == "review_a" and recovered.profile_error
    assert recovered.profile.fallback_profile_id == "review_b"
    assert recovered.harness == initial.harness
