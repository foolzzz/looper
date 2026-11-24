"""Processor recovery preserves the logical call's input, deadline and complete spend."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import pytest

from looper.agents import failures, processor
from looper.agents.agent_settings import AgentType


@pytest.fixture
def recovery(monkeypatch, tmp_path):
    from looper.agents import failure_handling, untrusted

    clock = NS(now=0.0)
    waits = []

    async def wait(delay, **kwargs):
        waits.append(delay)
        clock.now += delay
        return True

    service = NS(known_mode=lambda: failures.FailureTyping.ACT, wait_turn=wait,
                 quota_delay=lambda failure, backoff: min(300 * 2 ** (backoff - 1), 3600))
    monkeypatch.setattr(failure_handling, "get_service", lambda: service)
    monkeypatch.setattr(failures, "typing_mode", AsyncMock(return_value=failures.FailureTyping.ACT))
    monkeypatch.setattr(processor.time, "monotonic", lambda: clock.now)
    monkeypatch.setattr(processor.random, "uniform", lambda low, high: 1.0)
    settings = NS(max_budget_usd=None)
    monkeypatch.setattr(processor, "load_agent_settings", AsyncMock(return_value=settings))
    monkeypatch.setattr(processor, "_role", AsyncMock(return_value=NS(timeout_seconds=0)))
    monkeypatch.setattr(processor, "_managed_hooks", lambda settings: None)
    monkeypatch.setattr(untrusted, "enabled", AsyncMock(return_value=False))
    arguments = dict(owner="recovery", instructions="Summarize.", input_text="private batch",
                     input_ref={"seq": 1}, quiet_word="NONE", budget_usd=1.0, timeout_s=1000,
                     runtime_root=tmp_path, role=AgentType.PROCESSOR)
    return arguments, clock, waits, service, settings


@pytest.mark.parametrize("role", [AgentType.PROCESSOR, AgentType.LEADS_CHECK])
def test_processor_retry_deadline_cost_and_privacy(recovery, monkeypatch, role):
    arguments, clock, waits, service, settings = recovery
    calls = []
    transient = failures.Failure(failures.FailureCause.CONNECTION_FAILED)

    async def attempt(**kwargs):
        calls.append(kwargs)
        clock.now += 2
        if len(calls) < 3:
            return processor.ProcessorRun(None, len(calls), 0.1, processor.ProcessorFailure.FAILED,
                                          "its run ended in error", engine_failure=transient)
        return processor.ProcessorRun("Summary", 3, 0.2)

    monkeypatch.setattr(processor, "_run_once", attempt)
    done = asyncio.run(processor.run(**{**arguments, "role": role}))
    assert done.reply == "Summary" and done.cost_usd == pytest.approx(0.4)
    assert waits == [120, 600]
    assert len(calls) == 3
    assert [call["budget_usd"] for call in calls] == pytest.approx([1, 0.9, 0.8])
    assert [call["timeout_s"] for call in calls] == pytest.approx([1000, 878, 276])
    for call in calls:
        assert call["input_text"] == arguments["input_text"]
        assert call["input_ref"] == arguments["input_ref"]
        assert call["role"] is role
        assert call["_prepared"].settings is settings
    processor.load_agent_settings.assert_awaited_once()
    processor._role.assert_awaited_once()


def test_processor_stops_after_third_transient_and_accounts_failed_spend(recovery, monkeypatch):
    arguments, clock, waits, _, _ = recovery
    attempt = AsyncMock(return_value=processor.ProcessorRun(
        None, 7, 0.1, processor.ProcessorFailure.FAILED, "failed",
        engine_failure=failures.Failure(failures.FailureCause.CONNECTION_FAILED)))
    monkeypatch.setattr(processor, "_run_once", attempt)
    done = asyncio.run(processor.run(**arguments))
    assert attempt.await_count == 3 and waits == [120, 600]
    assert done.failure is processor.ProcessorFailure.FAILED
    assert done.cost_usd == pytest.approx(0.3)


@pytest.mark.parametrize("failure", [processor.ProcessorFailure.REFUSED, processor.ProcessorFailure.BLOCKED,
                                    processor.ProcessorFailure.BUDGET, processor.ProcessorFailure.TIMEOUT])
def test_processor_does_not_retry_policy_or_budget_failures(recovery, monkeypatch, failure):
    arguments, _, waits, _, _ = recovery
    attempt = AsyncMock(return_value=processor.ProcessorRun(
        None, 7, 0.1, failure, "refused", engine_failure=failures.Failure(failures.FailureCause.CONNECTION_FAILED)))
    monkeypatch.setattr(processor, "_run_once", attempt)
    done = asyncio.run(processor.run(**arguments))
    assert attempt.await_count == 1 and waits == []
    assert done.failure is failure and done.cost_usd == 0.1


def test_processor_quota_cannot_extend_caller_deadline_or_hide_attempt_cost(recovery, monkeypatch):
    arguments, clock, waits, _, _ = recovery
    attempt = AsyncMock(return_value=processor.ProcessorRun(
        None, 7, 0.1, processor.ProcessorFailure.FAILED, "quota",
        engine_failure=failures.Failure(failures.FailureCause.USAGE_LIMIT)))
    monkeypatch.setattr(processor, "_run_once", attempt)
    done = asyncio.run(processor.run(**{**arguments, "timeout_s": 250}))
    assert attempt.await_count == 1 and waits == [250]
    assert done.failure is processor.ProcessorFailure.TIMEOUT
    assert done.cost_usd == 0.1 and done.engine_failure.cls is failures.FailureClass.QUOTA


def test_processor_mode_exit_returns_original_failure_without_another_attempt(recovery, monkeypatch):
    arguments, _, waits, service, _ = recovery
    service.wait_turn = AsyncMock(return_value=False)
    attempt = AsyncMock(return_value=processor.ProcessorRun(
        None, 7, 0.1, processor.ProcessorFailure.FAILED, "transient",
        engine_failure=failures.Failure(failures.FailureCause.CONNECTION_FAILED)))
    monkeypatch.setattr(processor, "_run_once", attempt)
    done = asyncio.run(processor.run(**arguments))
    assert attempt.await_count == 1 and done.detail == "transient" and done.cost_usd == 0.1


def test_processor_caller_cancellation_interrupts_wait(recovery, monkeypatch):
    arguments, _, _, service, _ = recovery
    entered = asyncio.Event()

    async def wait(delay, **kwargs):
        entered.set()
        await asyncio.Future()

    service.wait_turn = wait
    attempt = AsyncMock(return_value=processor.ProcessorRun(
        None, 7, 0.1, processor.ProcessorFailure.FAILED, "transient",
        engine_failure=failures.Failure(failures.FailureCause.CONNECTION_FAILED)))
    monkeypatch.setattr(processor, "_run_once", attempt)

    async def run():
        task = asyncio.create_task(processor.run(**arguments))
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(run())
    assert attempt.await_count == 1


@pytest.mark.parametrize("mode", [failures.FailureTyping.OFF, failures.FailureTyping.RECORD])
def test_processor_disabled_recovery_preserves_single_attempt(recovery, monkeypatch, mode):
    arguments, _, _, _, _ = recovery
    monkeypatch.setattr(failures, "typing_mode", AsyncMock(return_value=mode))
    done = processor.ProcessorRun("same", 7, 0.1)
    attempt = AsyncMock(return_value=done)
    monkeypatch.setattr(processor, "_run_once", attempt)
    assert asyncio.run(processor.run(**arguments)) is done
    assert {key: attempt.await_args.kwargs[key] for key in arguments} == arguments
    assert "_prepared" not in attempt.await_args.kwargs
