"""A scheduled Planner bounds the conversation before doing the next run's work."""

import asyncio
from unittest.mock import AsyncMock

import pytest

from looper.agents.claude_cli import ResidentClaude, ResidentTurn
from looper.agents.run_usage import Usage
from looper.agents.runtimes import CLAUDE_CODE, Capability
from looper.db import schedules, tasks


@pytest.mark.parametrize("context,cost,budget,compact,outcome,rate", [
    (100_001, 0.1, 1, "success", "compacted", .1),
    (100_001, 0.1, 1, "unsupported", "rotated", .1),
    (100_001, 0.1, 1, "error", "rotated", .1),
    (100_001, 0.1, 1, "no_boundary", "rotated", .1),
    (100_000, 0.5, 1, "success", None, .1),
    (50_000, 0.5001, 1, "success", "compacted", .1),
    (50_000, 1, None, "success", None, .1),
    (None, None, 1, "success", "rotated", .1),
    (369_000, 0.1, 1, "success", "rotated", .1),
    (300_000, 0.1, 1, "success", "compacted", .1),
    (100_001, 0.1, 1, "success", "rotated", 10),
    (50_000, 0.6, 1, "success", "compacted", 10),
    (49_999, 0.9, 1, "unsupported", None, .1),
    (50_000, None, 1, "success", "rotated", .1),
    (100_001, 0.1, 1, "success", "compacted", None),
    (300_000, 0.1, 1, "success", "compacted", None),
    (300_001, 0.1, 1, "success", "rotated", None),
    (49_999, None, 1, "unsupported", None, .1),
])
def test_next_run_bounds_only_an_expensive_stored_session(
    monkeypatch, agent_db, planner_claude, context, cost, budget, compact, outcome, rate,
):
    from looper.agents import scheduler_settings
    from looper.agents.agent_settings import Launch
    from looper.agents.runtimes.pricing import ModelPrice, PriceSnapshot
    from tests.test_planner_agent import WORK_OK, _project, always, decide, drive, plan, step

    agent_db.get_task_project.return_value = {**_project(), "task_schedule_id": "sched-a",
                                            "task_budget_usd": budget}
    claim = AsyncMock(return_value={
        "claimed": True, "session_id": "sess-p", "session_totals": {},
        "runtime_type": "claude-code", "profile_id": None,
        "provider_id": "prov-claude-subscription", "previous": None,
        "context_tokens": context, "run_cost_usd": cost,
    })
    # None exercises native OAuth pricing, without replacing Launch.price.
    if rate is not None:
        monkeypatch.setattr(Launch, "price", property(lambda launch: PriceSnapshot(
            {launch.model: ModelPrice(rate, rate, rate, rate)}, {}, {}, {})))
    monkeypatch.setattr(schedules, "claim_planner_session", claim)
    monkeypatch.setattr(tasks, "set_continues_schedule_session", AsyncMock())
    monkeypatch.setattr(schedules, "save_planner_session", AsyncMock())
    bounded = AsyncMock()
    telemetry = AsyncMock()
    monkeypatch.setattr(schedules, "record_planner_session_bound", bounded, raising=False)
    monkeypatch.setattr(schedules, "record_planner_session_run", telemetry, raising=False)
    monkeypatch.setattr(scheduler_settings, "session_context_max_tokens", AsyncMock(return_value=100_000), raising=False)
    capabilities = set(CLAUDE_CODE.capabilities)
    capability = getattr(Capability, "HEADLESS_COMPACTION", None)
    if compact == "unsupported" and capability:
        capabilities.discard(capability)
    monkeypatch.setattr(CLAUDE_CODE, "capabilities", frozenset(capabilities))
    original = ResidentClaude.run_turn
    compactions = []

    async def run_turn(self, text, on_event, **kwargs):
        if text != "/compact":
            async def recorded(event):
                if event.get("type") == "result":
                    await on_event({"type": "assistant", "message": {"usage": {
                        "input_tokens": 10, "cache_read_input_tokens": 100, "cache_creation_input_tokens": 30}}})
                    await on_event({"type": "assistant", "parent_tool_use_id": "child-call", "message": {
                        "usage": {"input_tokens": 999_999}}})
                await on_event(event)
            return await original(self, text, recorded, **kwargs)
        compactions.append(text)
        if compact == "error":
            raise RuntimeError("synthetic compaction failure")
        if compact != "no_boundary":
            await on_event({"type": "system", "subtype": "compact_boundary",
                            "compact_metadata": {"trigger": "manual", "pre_tokens": context, "post_tokens": 4000}})
        result = {"type": "result", "subtype": "success", "result": "", "session_id": "sess-p"}
        await on_event(result)
        return ResidentTurn(result, usage=Usage(cost_usd=0.01))

    monkeypatch.setattr(ResidentClaude, "run_turn", run_turn)
    claude = planner_claude(plan(step("new-step")), decide("accept"))
    h = drive(claude, always(WORK_OK))
    assert h.final().payload["status"] == "completed"
    if outcome:
        assert bounded.await_count == 1
        facts = bounded.await_args.kwargs
        assert facts["action"] == outcome
        assert facts["reason"] == ("context" if context is None or context > 100_000 else "cost")
        assert facts["before_tokens"] == context
        assert facts["after_tokens"] == (4000 if outcome == "compacted" else None)
        direct = (context is None or context > 300_000 or cost is None
                  or (isinstance(rate, (int, float)) and context * rate / 1e6 > (budget or float("inf")) / 2))
        assert bool(compactions) is (compact != "unsupported" and not direct)
        if outcome == "rotated":
            assert "--resume" not in claude.argvs()[-1]
            assert "Recent scheduled run results" not in claude.turns()[0]["text"]
    else:
        bounded.assert_not_awaited()
        assert not compactions
        assert "--resume" in claude.argvs()[0]
    assert telemetry.await_count >= 1
    assert telemetry.await_args.kwargs["context_tokens"] == 140


@pytest.mark.parametrize("runtime,rotates", [
    ("claude-code", True), ("omp", True), ("pi", True), ("api", True),
    ("codex", False), ("cursor-agent", False), ("grok", False), ("coco", False), ("dsh", False),
])
def test_unknown_context_preserves_runtimes_without_request_measurements(monkeypatch, agent_db, tmp_path,
                                                                         runtime, rotates):
    from looper.agents.tiers.planner import PlannerAgent, TaskPlan

    monkeypatch.setattr(tasks, "set_continues_schedule_session", AsyncMock())
    monkeypatch.setattr(schedules, "claim_planner_session", AsyncMock(return_value={
        "claimed": True, "session_id": "stored", "session_totals": {}, "runtime_type": runtime,
        "provider_id": None, "profile_id": None, "previous": None,
        "context_tokens": None, "run_cost_usd": None,
    }))
    agent = PlannerAgent(task_id="next", runtime_root=tmp_path)
    agent._scheduled = True
    plan = TaskPlan("plan", "goal", "Read", str(tmp_path))
    asyncio.run(agent._claim_schedule_session(plan))
    assert plan.session_id == "stored" and agent._schedule_session_writes
    assert (agent._schedule_bound is not None) is rotates


def test_replanning_does_not_reuse_a_previous_rotation_decision(monkeypatch, agent_db, tmp_path):
    from looper.agents.schedule_session import BoundReason, SessionBound
    from looper.agents.tiers.planner import PlannerAgent, TaskPlan

    monkeypatch.setattr(tasks, "set_continues_schedule_session", AsyncMock())
    monkeypatch.setattr(schedules, "claim_planner_session", AsyncMock(return_value={"claimed": False, "reason": "parallel"}))
    agent = PlannerAgent(task_id="replanned", runtime_root=tmp_path)
    agent._scheduled = True
    agent._schedule_bound = SessionBound(BoundReason.CONTEXT, 120_000)
    plan = TaskPlan("plan", "goal", "Read", str(tmp_path))
    asyncio.run(agent._claim_schedule_session(plan))
    assert agent._schedule_bound is None
