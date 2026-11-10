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


@pytest.fixture
def planner_records(agent_db):
    records = {}

    async def start(**fields):
        record_id = len(records) + 1
        records[record_id] = fields
        return record_id

    async def annotate(record_id, metadata):
        records[record_id]["metadata"].update(metadata)

    agent_db.start_record.side_effect = start
    agent_db.add_metadata.side_effect = annotate
    return records


@pytest.mark.parametrize("outcome", ["success", "command_error", "start_error", "turn_error"])
def test_planner_run_keeps_full_fallback_chain(
    planner_claude, agent_db, planner_records, monkeypatch, tmp_path, outcome,
):
    from looper.agents.agent_settings import Launch

    async def run():
        claude = planner_claude("<error>" if outcome == "turn_error" else "Inspected")
        agent = module.PlannerAgent(task_id="fallback-chain", runtime_root=tmp_path, claude_bin=claude.bin)
        agent._plan = module.TaskPlan("plan", "goal", "Inspect", str(tmp_path))
        profiles = [replace(builtin_resolved_profile(), id=name) for name in ("primary", "middle", "final")]
        selected = [AgentSettings(profile=p, profile_error=None if p.id == "final" else "disabled") for p in profiles]
        monkeypatch.setattr(agent, "_load_agent_settings", AsyncMock(return_value=selected[0]))
        monkeypatch.setattr(module, "fallback_settings", AsyncMock(side_effect=selected[1:]))
        monkeypatch.setattr(Launch, "environment", lambda self: {})
        if outcome == "command_error":
            monkeypatch.setattr(agent, "_claude_command", lambda *args: (_ for _ in ()).throw(OSError("rejected")))
        elif outcome == "start_error":
            monkeypatch.setattr(agent._claude, "start", AsyncMock(side_effect=OSError("cannot start")))
        try:
            if outcome == "success":
                assert await agent._turn("Inspect") == "Inspected"
            else:
                with pytest.raises(module.PlannerTurnError):
                    await agent._turn("Inspect")
            record, = planner_records.values()
            assert record["metadata"]["profile"]["id"] == "final"
            assert record["metadata"].get("fallback") == [
                {"from": "primary", "to": "middle", "reason": "profile_error"},
                {"from": "middle", "to": "final", "reason": "profile_error"},
            ]
            assert record["metadata"]["fallback_from"] == "middle"
        finally:
            await agent._claude.stop()

    asyncio.run(run())


def test_fallback_belongs_to_target_runs_and_excludes_old_session_handoff(
    planner_claude, agent_db, planner_records, monkeypatch, tmp_path,
):
    from looper.agents.agent_settings import Launch

    async def run():
        claude = planner_claude("First", '```json\n{"handoff": "Continue inspecting"}\n```', "Recovered", "Again", "Direct")
        agent = module.PlannerAgent(task_id="fallback-session", runtime_root=tmp_path, claude_bin=claude.bin)
        agent._plan = module.TaskPlan("plan", "goal", "Inspect", str(tmp_path))
        primary = AgentSettings(profile=replace(builtin_resolved_profile(), id="primary"))
        resolved = primary.profile.resolved_model
        backup = AgentSettings(profile=replace(primary.profile, id="backup", resolved_model=replace(
            resolved, provider=replace(resolved.provider, id="backup-provider"),
            model=replace(resolved.model, provider_id="backup-provider"),
            endpoint=replace(resolved.endpoint, provider_id="backup-provider"))))
        disabled = replace(primary, profile_error="provider disabled")
        monkeypatch.setattr(agent, "_load_agent_settings", AsyncMock(side_effect=[primary, disabled, disabled, backup]))
        monkeypatch.setattr(module, "fallback_settings", AsyncMock(return_value=backup))
        monkeypatch.setattr(Launch, "environment", lambda self: {})
        try:
            for reply in ("First", "Recovered", "Again", "Direct"):
                assert await agent._turn("Inspect") == reply
            records = list(planner_records.values())
            assert len(records) == 5
            assert "[HANDOFF]" in records[1]["input_text"]
            assert [r["metadata"]["profile"]["id"] for r in records] == ["primary", "primary", "backup", "backup", "backup"]
            for index, record in enumerate(records):
                if index in (2, 3):
                    assert record["metadata"].get("fallback") == [
                        {"from": "primary", "to": "backup", "reason": "profile_error"}]
                else:
                    assert "fallback" not in record["metadata"]
                    assert "fallback_from" not in record["metadata"]
        finally:
            await agent._claude.stop()

    asyncio.run(run())


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


@pytest.mark.parametrize(("runtime", "protocol"), [("pi", "openai-chat"), ("dsh", "anthropic-messages")])
def test_runner_budget_uses_launched_fallback_profile(agent_db, monkeypatch, tmp_path, runtime, protocol):
    from agent_fixtures import profile_json
    from looper.agents.agent_settings import AgentType, Launch, fallback_settings, load_agent_settings
    from looper.agents.claude_cli import ResidentTurn
    from looper.agents.project_execution import ProjectExecution
    from looper.agents.runtimes import ADAPTERS
    from looper.agents.runtimes.dsh_runner import DshRunnerRuntime
    from looper.agents.runtimes.pi_runner import PiRunnerRuntime
    from looper.agents.runtimes.runner.session import RunnerSession
    from looper.db import agent_setting_layers, profile_registry

    monkeypatch.setitem(ADAPTERS, runtime, PiRunnerRuntime(None, pi=None) if runtime == "pi" else
                        DshRunnerRuntime(None, dsh=None))
    primary = profile_json("primary")
    primary["provider"]["enabled"] = False
    backup = profile_json("deepseek-flash")
    backup.update(runtime_id=f"rt-{runtime}", runtime_name=runtime, runtime_type=runtime)
    backup["provider"].update(id="backup-provider", auth="api_key", vendor_type="custom", oauth_runtime_type=None)
    backup["model"]["provider_id"] = backup["endpoint"]["provider_id"] = "backup-provider"
    backup["model"]["protocol"] = backup["endpoint"]["protocol"] = protocol
    backup["endpoint"]["base_url"] = "https://provider.example.invalid"
    primary["fallback_profile_id"] = backup["id"]
    layer = {"scope": "role", "role": "planner", "profile": primary, "run_budget_usd": 2}
    monkeypatch.setattr(agent_setting_layers, "list_layers", AsyncMock(return_value=[layer]))
    profiles = {profile["id"]: profile for profile in (primary, backup)}
    monkeypatch.setattr(profile_registry, "get_profile", AsyncMock(side_effect=profiles.get))
    monkeypatch.setattr(profile_registry, "get_api_key", AsyncMock(return_value="test-only"))
    monkeypatch.setattr(profile_registry, "list_models", AsyncMock(return_value=[backup["model"]]))

    async def run():
        selected = await load_agent_settings(AgentType.PLANNER)
        assert selected.profile_error and "disabled" in selected.profile_error
        recovered = await fallback_settings(selected, AgentType.PLANNER)
        assert recovered is not None and recovered.profile_error is None
        launch = Launch(recovered, role=AgentType.PLANNER)
        agent = module.PlannerAgent(task_id="fallback-budget", runtime_root=tmp_path)
        agent._plan = module.TaskPlan("plan", "goal", "Inspect", str(tmp_path))
        session = RunnerSession(agent.agent_id, None, runtime)
        agent._claude = session
        monkeypatch.setattr(agent, "_read_execution", AsyncMock(return_value=ProjectExecution(task_budget_usd=8)))
        monkeypatch.setattr(module.db_llm_records, "task_costs",
                            AsyncMock(return_value={"fallback-budget": {"cost_usd": 3}}))

        async def run_turn(*args, **kwargs):
            # Authorize requests during the turn, including after a live budget change.
            assert await session._budget["limits"]() == {"run_cap": 2, "task_cap": 8, "task_spent": 3}
            layer["run_budget_usd"] = 1
            assert await session._budget["limits"]() == {"run_cap": 1, "task_cap": 8, "task_spent": 3}
            return ResidentTurn({"subtype": "success", "result": "Inspected"})

        monkeypatch.setattr(session, "run_turn", run_turn)
        process = module._Process([], None, module.SystemPrompt("Inspect"), launch)
        assert await agent._exchange("Inspect", process, 2) == "Inspected"
        assert session._budget["provider_id"] == "backup-provider"
        assert agent_db.finish_record.call_args.kwargs["error"] is None

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
