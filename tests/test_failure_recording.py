"""A run record's failure class (``ClaudeRunRecorder`` with ``failures``), against the recorded database writes."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

FIXTURES = Path(__file__).parent / "fixtures" / "failures"


def _launch() -> Any:
    from looper.agents.agent_settings import AgentSettings, Launch
    from looper.agents.profile_registry import builtin_resolved_profile

    return Launch(AgentSettings(profile=builtin_resolved_profile()))


def _recorder(agent_id: str = "worker-failure") -> Any:
    from looper.agents.claude_cli import ClaudeRunRecorder

    return ClaudeRunRecorder(agent_id=agent_id, task_id="task-failure", command=["claude"], cwd="/tmp",
                             input_text="the step", launch=_launch(), whole_process=True)


def _sample(name: str) -> list[dict[str, Any]]:
    return [json.loads(line) for line in (FIXTURES / name).read_text().splitlines()]


def _run(events: str | list[dict[str, Any]], *, outcome: str | None = "run_failed",
         error: str | None = "the run failed", failure: Any = None) -> None:
    events = _sample(events) if isinstance(events, str) else events
    result = next(event for event in events if event["type"] == "result")

    async def run() -> None:
        async with _recorder() as recorder:
            for event in events:
                await recorder.record(event)
            await recorder.finish(output_text=None, result_event=result, error=error, outcome=outcome,
                                  failure=failure)

    asyncio.run(run())


def _closed(agent_db: Any) -> dict[str, Any]:
    return agent_db.finish_record.await_args.kwargs


@pytest.fixture
def typing(agent_db: Any):
    def set_mode(value: str | None) -> None:
        agent_db.get_settings.return_value = {} if value is None else {"engine.failure_typing": value}
    return set_mode


@pytest.mark.parametrize("mode", [None, "off"])
def test_off_records_exactly_what_it_did_before(agent_db, typing, mode):
    typing(mode)
    _run("claude_usage_limit.jsonl")
    off = _closed(agent_db)
    assert "failure_class" not in off and "failure_cause" not in off
    assert "failure" not in (off.get("metadata") or {})

    typing("record")
    _run("claude_usage_limit.jsonl")
    on = _closed(agent_db)
    added = {"failure_class": "quota", "failure_cause": "usage_limit"}
    assert {key: on[key] for key in added} == added
    assert on["metadata"].pop("failure") == {"signal": "typed", "resets_at": "2027-01-15T08:00:00+00:00"}
    # Apart from the class, the close is the same write (its duration aside).
    assert {k: v for k, v in on.items() if k not in (*added, "duration_ms", "metadata")} == \
        {k: v for k, v in off.items() if k not in ("duration_ms", "metadata")}
    assert (on.get("metadata") or None) == (off.get("metadata") or None)


def test_record_classifies_a_failed_run_and_keeps_looper_s_own_reason(agent_db, typing):
    typing("record")
    _run("claude_connection_failed.jsonl")
    assert (_closed(agent_db)["failure_class"], _closed(agent_db)["failure_cause"]) == ("transient",
                                                                                        "connection_failed")
    from looper.agents import failures

    _run("claude_connection_failed.jsonl", failure=failures.TIMED_OUT)
    assert (_closed(agent_db)["failure_class"], _closed(agent_db)["failure_cause"]) == ("judgment", "timeout")


def test_a_report_that_says_the_step_failed_is_a_judgment_and_a_success_has_no_class(agent_db, typing):
    typing("record")
    success = [{"type": "result", "subtype": "success", "is_error": False, "result": "{}", "num_turns": 1}]
    _run(success, outcome="reported_failed", error=None)
    assert (_closed(agent_db)["failure_class"], _closed(agent_db)["failure_cause"]) == ("judgment", "report")
    _run(success, outcome="completed", error=None)
    assert "failure_class" not in _closed(agent_db)
    _run(success, outcome="not_passed", error=None)  # a verdict, not a failure
    assert "failure_class" not in _closed(agent_db)


def test_a_run_looper_stopped_has_no_class_and_one_its_handling_broke_is_unknown(agent_db, typing):
    typing("record")

    async def stopped() -> None:
        async with _recorder() as recorder:
            await recorder.record({"type": "system", "subtype": "init", "session_id": "s"})
            raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(stopped())
    closed = _closed(agent_db)
    assert closed["status"] == "interrupted" and "failure_class" not in closed

    async def broken() -> None:
        async with _recorder() as recorder:
            await recorder.record({"type": "system", "subtype": "init", "session_id": "s"})
            raise RuntimeError("Looper's own bug")

    with pytest.raises(RuntimeError):
        asyncio.run(broken())
    closed = _closed(agent_db)
    assert closed["status"] == "failed"
    assert (closed["failure_class"], closed["failure_cause"]) == ("judgment", "unknown")


def test_a_processor_run_s_rate_limit_event_is_seen_before_the_record_s_filter(agent_db, typing):
    """The processor keeps only system and result events, but the class still sees the rejected window."""
    from looper.agents.claude_cli import ClaudeRunRecorder
    from looper.agents.processor import typed_events

    typing("record")
    events = _sample("claude_usage_limit_older.jsonl")

    async def run() -> None:
        async with ClaudeRunRecorder(agent_id="processor:sched-x", task_id=None, command=["claude"], cwd="/tmp",
                                     input_text="window", launch=_launch(), whole_process=True,
                                     keep=typed_events) as recorder:
            for event in events:
                await recorder.record(event)
            await recorder.finish(output_text=None, result_event=events[-1], error="no result")

    asyncio.run(run())
    stored = [call.args[1][0][1]["type"] for call in agent_db.append_events.await_args_list]
    assert "rate_limit_event" not in stored
    assert (_closed(agent_db)["failure_class"], _closed(agent_db)["failure_cause"]) == ("quota", "usage_limit")


def test_recorder_feeds_admission_once_with_the_classified_failure(agent_db, typing, monkeypatch):
    from unittest.mock import AsyncMock
    from looper.agents import admission, failures

    typing("record")
    monkeypatch.setattr(admission, "enabled", lambda: True)
    saved = AsyncMock()
    monkeypatch.setattr(admission, "recorded", saved)
    _run("claude_usage_limit.jsonl")
    saved.assert_awaited_once()
    provider, owner, record_id, failure = saved.await_args.args
    assert provider == "prov-claude-subscription" and owner == "worker-failure"
    assert record_id is not None and failure.cls is failures.FailureClass.QUOTA
    assert saved.await_args.kwargs == {"interrupted": False}

    saved.reset_mock()
    _run([{"type": "result", "subtype": "success", "result": "{}"}], error=None, outcome="completed")
    assert saved.await_args.args[3] is None  # successful probes also report their end
