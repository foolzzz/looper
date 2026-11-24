"""A Planner's runtime retry reuses the admitted exchange and its original input."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock
from datetime import datetime, timedelta, timezone

import pytest

from looper.agents import failures
from looper.agents import admission, turn_admission
from looper.agents.tiers import planner
from looper.agents.tiers.planner_protocol import TurnKind


@pytest.fixture
def recovery(monkeypatch, tmp_path, agent_db):
    from looper.agents import failure_handling

    waits = []

    async def wait(delay, **kwargs):
        waits.append(delay)
        return True

    service = NS(known_mode=lambda: failures.FailureTyping.ACT, wait_turn=wait)
    monkeypatch.setattr(failure_handling, "get_service", lambda: service)
    monkeypatch.setattr(failures, "typing_mode", AsyncMock(return_value=failures.FailureTyping.ACT))
    monkeypatch.setattr(planner.random, "uniform", lambda low, high: 1.0)
    monkeypatch.setattr(planner.subagent_delivery, "planner_turn_note", AsyncMock(return_value=("\nNOTE", [])))
    agent = planner.PlannerAgent(task_id="retry", runtime_root=tmp_path)
    agent._plan = planner.TaskPlan("p", "g", "Inspect", str(tmp_path))
    agent._claude = NS(started=True)
    return agent, service, waits


@pytest.mark.parametrize("kind", [TurnKind.PLAN, TurnKind.DECISION, TurnKind.CHAT])
def test_planner_retries_same_exchange_outside_admission(recovery, monkeypatch, kind):
    agent, _, waits = recovery
    process, calls, active = object(), [], []
    agent._turn_kind = kind
    transient = failures.Failure(failures.FailureCause.CONNECTION_FAILED)

    async def exchange(text, given, limit, **kwargs):
        active.append(True)
        try:
            calls.append((text, given, limit, kwargs))
            if len(calls) < 3:
                raise planner.PlannerTurnError("connection failed", failure=transient)
            return "answer"
        finally:
            active.pop()

    async def wait(delay, **kwargs):
        assert not active
        waits.append(delay)
        return True

    recovery[1].wait_turn = wait
    monkeypatch.setattr(agent, "_exchange_once", exchange)
    assert asyncio.run(agent._exchange("[REQUEST]\nexact bytes", process, 30)) == "answer"
    assert waits == [120, 600]
    assert len(calls) == 3 and all(call == calls[0] for call in calls)
    assert calls[0][0] == "[REQUEST]\nexact bytes\nNOTE"
    assert calls[0][1] is process
    planner.subagent_delivery.planner_turn_note.assert_awaited_once()
    assert agent._admission_recovered


def test_planner_stops_on_third_transient_and_mode_exit(recovery, monkeypatch):
    agent, service, waits = recovery
    exc = planner.PlannerTurnError("connection failed", failure=failures.Failure(failures.FailureCause.CONNECTION_FAILED))
    exchange = AsyncMock(side_effect=exc)
    monkeypatch.setattr(agent, "_exchange_once", exchange)
    with pytest.raises(planner.PlannerTurnError) as raised:
        asyncio.run(agent._exchange("same", object(), 30))
    assert raised.value is exc and exchange.await_count == 3 and waits == [120, 600]
    exchange.reset_mock()
    service.wait_turn = AsyncMock(return_value=False)
    with pytest.raises(planner.PlannerTurnError) as raised:
        asyncio.run(agent._exchange("same", object(), 30))
    assert raised.value is exc and exchange.await_count == 1


def test_planner_environment_error_has_terminal_reason_only_for_plan(recovery, monkeypatch):
    agent, _, waits = recovery
    failure = failures.Failure(failures.FailureCause.RUNTIME_MISSING)
    for kind in (TurnKind.PLAN, TurnKind.DECISION, TurnKind.CHAT):
        error = planner.PlannerTurnError("missing runtime", failure=failure)
        monkeypatch.setattr(agent, "_exchange_once", AsyncMock(side_effect=error))
        agent._turn_kind = kind
        with pytest.raises(planner.PlannerTurnError) as raised:
            asyncio.run(agent._exchange("same", object(), 30))
        assert raised.value.failure is failure
        assert raised.value.fail_kind == (planner.FailKind.ENVIRONMENT if kind is TurnKind.PLAN else None)
    assert waits == []


@pytest.mark.parametrize("mode", [failures.FailureTyping.OFF, failures.FailureTyping.RECORD])
def test_planner_disabled_handling_calls_unchanged_exchange(recovery, monkeypatch, mode):
    agent, _, waits = recovery
    monkeypatch.setattr(failures, "typing_mode", AsyncMock(return_value=mode))
    call = AsyncMock(return_value="same")
    monkeypatch.setattr(agent, "_exchange_once", call)
    process = object()
    assert asyncio.run(agent._exchange("text", process, 30)) == "same"
    assert call.await_args.args == ("text", process, 30)
    assert call.await_args.kwargs == {}
    planner.subagent_delivery.planner_turn_note.assert_not_called()
    assert waits == []


def test_protocol_repair_shares_the_logical_runtime_failure_budget(recovery, monkeypatch):
    agent, _, waits = recovery
    failure = failures.Failure(failures.FailureCause.CONNECTION_FAILED)
    exchange = AsyncMock(side_effect=[planner.PlannerTurnError("failed", failure=failure),
                                      planner.PlannerTurnError("failed", failure=failure),
                                      "invalid contract", planner.PlannerTurnError("failed", failure=failure)])
    monkeypatch.setattr(agent, "_exchange_once", exchange)
    monkeypatch.setattr(agent, "_read_run_deadline", AsyncMock())
    monkeypatch.setattr(agent, "_read_roles", AsyncMock())

    def parse(text):
        raise planner.ProtocolError("invalid")

    async def run(text):
        return await agent._exchange(text, object(), 30)

    with pytest.raises(planner.PlannerTurnError):
        asyncio.run(agent._ask(TurnKind.PLAN, "original", parse, run))
    assert exchange.await_count == 4 and waits == [120, 600]
    assert exchange.await_args_list[0].args[0] == exchange.await_args_list[2].args[0]
    assert "reply contract" in exchange.await_args_list[3].args[0]


@pytest.mark.parametrize("elapsed", [False, True])
def test_frozen_initial_admission_has_deadline_and_cancellation_without_a_model_call(recovery, monkeypatch, elapsed):
    agent, service, _ = recovery
    agent._scheduled = True
    agent._quota_failure_deadline = AsyncMock(return_value=datetime.now(timezone.utc) + timedelta(seconds=10))
    service.wait_turn = AsyncMock(return_value=elapsed)
    monkeypatch.setattr(admission, "enabled", lambda: True)
    monkeypatch.setattr(admission, "worker_lane", AsyncMock(return_value=admission.Lane.BACKGROUND))
    stopped = []

    @asynccontextmanager
    async def slot(provider, lane, **kwargs):
        await kwargs["on_frozen"](object())
        try:
            await asyncio.Future()
            yield
        finally:
            stopped.append(True)

    monkeypatch.setattr(admission, "slot", slot)
    exchange = AsyncMock()
    process = NS(launch=NS(profile=NS(provider=NS(id="synthetic"))))
    with pytest.raises(planner.PlannerTurnError) as raised:
        asyncio.run(turn_admission.admitted_planner_exchange(exchange)(agent, "same", process, 30))
    assert raised.value.fail_kind == (planner.FailKind.QUOTA_EXHAUSTED if elapsed else None)
    assert raised.value.failure.cls is failures.FailureClass.QUOTA
    agent._quota_failure_deadline.assert_awaited_once()
    exchange.assert_not_awaited()
    assert stopped == [True]


def test_planner_start_timeout_retries_the_same_launch_and_counts_every_failed_attempt(recovery, monkeypatch):
    from looper.agents.runtimes.channel import RunStartTimeout

    agent, _, waits = recovery
    agent._claude.start = AsyncMock(side_effect=[RunStartTimeout("busy"), RunStartTimeout("busy"), None])
    recorders = []

    def recorder(*args):
        found = NS(start=AsyncMock(), finish=AsyncMock(), annotate=AsyncMock())
        recorders.append(found)
        return found

    monkeypatch.setattr(agent, "_recorder", recorder)
    process = NS(launch=NS(runtime=NS(label="Script")))
    kwargs = dict(cwd="/synthetic", signature="same", env={}, totals=None)
    asyncio.run(agent._start_for_turn(["script"], plan=agent._plan, text="same", process=process, **kwargs))
    assert waits == [120, 600] and len(recorders) == 2
    assert all(call.args == (["script"],) and call.kwargs == kwargs
               for call in agent._claude.start.await_args_list)
    for saved in recorders:
        assert saved.finish.await_args.kwargs["failure"].cause is failures.FailureCause.START_TIMEOUT


@pytest.mark.parametrize("kind", list(TurnKind))
def test_scheduled_quota_deadline_terminates_every_turn_without_accepting_a_successful_step(
        recovery, monkeypatch, kind):
    agent, _, _ = recovery
    agent._scheduled = True
    plan = agent._plan
    plan.status = "executing"
    step = planner.PlanStep("done", 0, "work", status="deciding", result={"status": "completed", "summary": "done"})
    plan.steps = [step]
    agent._bus = NS(publish=AsyncMock())
    monkeypatch.setattr(agent, "_clear_failure_holds", AsyncMock())
    monkeypatch.setattr(agent, "_read_run_deadline", AsyncMock())
    monkeypatch.setattr(agent, "_read_roles", AsyncMock())
    quota = planner.PlannerTurnError("quota deadline", failure=failures.Failure(failures.FailureCause.USAGE_LIMIT),
                                      fail_kind=planner.FailKind.QUOTA_EXHAUSTED)

    async def run():
        with pytest.raises(planner.PlannerTurnError):
            await agent._ask(kind, "same request", str, run=AsyncMock(side_effect=quota))
        if kind is TurnKind.DECISION:
            await agent._undecided(plan, step, planner.DecisionContext(
                "done", planner.ResultKind.WORK, True, False, 0), quota)

    asyncio.run(run())
    assert plan.status == "failed" and step.status == "deciding"
    messages = [call.args[0] for call in agent._bus.publish.await_args_list]
    assert len(messages) == 1
    assert messages[0].payload["status"] == "failed"
    assert messages[0].payload["fail_kind"] == "quota_exhausted"
