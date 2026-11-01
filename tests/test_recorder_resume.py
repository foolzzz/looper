"""A resumed recorder rebuilds usage and continues the existing transcript."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from looper.agents.claude_cli import ClaudeRunRecorder, RecordNotResumable
from looper.agents.profile_registry import BUILTIN_PROVIDER
from looper.agents.run_usage import Usage
from looper.agents.runtimes import CLAUDE_CODE
from looper.agents.runtimes.pricing import usage_event
from looper.db import llm_records


def test_resume_keeps_the_record_sequence_and_original_duration(monkeypatch: pytest.MonkeyPatch) -> None:
    created = datetime.now(timezone.utc) - timedelta(seconds=30)
    mocks = {}
    for name, value in (("record_state", {"status": "running", "created_at": created}),
                        ("max_event_seq", 7), ("start_record", 99), ("append_events", None),
                        ("touch_record", None), ("finish_record", None)):
        mocks[name] = AsyncMock(return_value=value)
        monkeypatch.setattr(llm_records, name, mocks[name])
    launch = SimpleNamespace(price=None, model="m", max_budget_usd=None, runtime=CLAUDE_CODE,
                             profile=SimpleNamespace(provider=BUILTIN_PROVIDER))

    async def run() -> None:
        recorder = await ClaudeRunRecorder.resume(41, agent_id="worker-kept", task_id="task-kept",
                                                  command=["claude"], cwd="/tmp", step_id="step-kept",
                                                  launch=launch, max_budget_usd=None)
        monkeypatch.setattr(recorder, "_after_run", AsyncMock())
        async with recorder:
            recorder.account({"type": "system", "subtype": "init", "model": "m", "session_id": "session"})
            recorder.account({"type": "assistant", "message": {"content": []}})
            mocks["append_events"].assert_not_awaited()
            await recorder.record({"type": "assistant", "message": {"content": []}})
            result = {"type": "result", "subtype": "success", "total_cost_usd": 0.75}
            recorder.account(result)
            assert recorder._result == result
            await recorder.finish(output_text="done", result_event=result)

    asyncio.run(run())
    mocks["start_record"].assert_not_awaited()
    assert mocks["append_events"].await_args.args == (41, [(8, {"type": "assistant", "message": {"content": []}})])
    fields = mocks["finish_record"].await_args.kwargs
    assert mocks["finish_record"].await_args.args == (41,)
    assert fields["status"] == "completed"
    assert fields["cost_usd"] == 0.75
    assert 30_000 <= fields["duration_ms"] < 35_000


def test_resume_uses_the_manifest_cap_with_the_rebuilt_prices(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(llm_records, "record_state", AsyncMock(return_value={
        "status": "running", "created_at": datetime.now(timezone.utc),
    }))
    monkeypatch.setattr(llm_records, "max_event_seq", AsyncMock(return_value=0))
    launch = SimpleNamespace(price=lambda model, usage: (usage.input_tokens or 0) / 1_000_000,
                             model="m", max_budget_usd=10)

    async def run() -> None:
        recorder = await ClaudeRunRecorder.resume(41, agent_id="worker-kept", task_id=None, command=["claude"],
                                                  cwd="/tmp", step_id=None, launch=launch, max_budget_usd=0.1)
        recorder.account(usage_event("m", Usage(input_tokens=200_000)))
        assert recorder.cap_stop is not None
        assert recorder._spend.cap == 0.1
        assert recorder._spend.usage.cost_usd == pytest.approx(0.2)

    asyncio.run(run())


@pytest.mark.parametrize("status", [None, "failed", "completed", "interrupted"])
def test_resume_rejects_a_missing_or_closed_record(monkeypatch: pytest.MonkeyPatch, status: str | None) -> None:
    monkeypatch.setattr(llm_records, "record_state", AsyncMock(return_value=None if status is None else {
        "status": status, "created_at": datetime.now(timezone.utc),
    }))
    last = AsyncMock()
    monkeypatch.setattr(llm_records, "max_event_seq", last)

    async def run() -> None:
        with pytest.raises(RecordNotResumable, match="record 41 is not running"):
            await ClaudeRunRecorder.resume(41, agent_id="worker-kept", task_id=None, command=["claude"],
                                           cwd="/tmp", step_id=None, launch=SimpleNamespace(), max_budget_usd=None)

    asyncio.run(run())
    last.assert_not_awaited()
