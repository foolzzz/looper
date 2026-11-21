"""The typed call contract and its subscription-login one-shot backend."""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from looper.agents import model_call
from looper.agents.agent_settings import AgentType, resolve
from looper.agents.profile_registry import builtin_resolved_profile
from looper.agents.spawn import ChildRun, SpawnKind, SpawnParent
from looper.agents.system_prompt import SystemPrompt
from tests.db_support import run_db
from tests.fake_claude import fake_claude


def _request(tmp_path: Path, **changes: Any) -> model_call.ChildCall:
    settings = replace(resolve(AgentType.ADVISOR, {}), profile=builtin_resolved_profile())
    child = ChildRun(SpawnKind.ADVISOR, SystemPrompt("Return the answer as JSON.", {}),
                     "What is six times seven?", timeout_s=15, role=AgentType.ADVISOR)
    return model_call.ChildCall(child=child, parent=SpawnParent(None), settings=settings,
                                cap_usd=0.2, runtime_root=tmp_path / "runtime", **changes)


class _Backend:
    def __init__(self, execution: model_call.Execution) -> None:
        self.execution = execution
        self.requests: list[Any] = []

    async def run(self, request: Any) -> model_call.Execution:
        self.requests.append(request)
        return self.execution


def test_a_call_parses_once_after_execution_and_cost_booking(tmp_path: Path) -> None:
    order: list[str] = []
    execution = model_call.Execution('{"answer":42}', record_id=7, cost_usd=0.02,
                                     models=("chosen-model",), profile={"profile_id": "chosen"})

    async def booked(done: model_call.Execution) -> None:
        assert done is execution
        order.append("booked")

    def parse(text: str) -> int:
        order.append("parsed")
        return json.loads(text)["answer"]

    request = _request(tmp_path, on_result=booked)
    backend = _Backend(execution)
    done = asyncio.run(model_call.call(request, parse=parse, backend=backend))
    assert backend.requests == [request] and order == ["booked", "parsed"]
    assert (done.value, done.failure, done.reason, done.parse_error) == (42, None, "", None)
    assert done.execution is execution
    assert (done.record_id, done.cost_usd, done.model) == (7, 0.02, "chosen-model")
    assert done.latency_ms >= 0
    assert done.execution.round() == {"record_id": 7, "cost_usd": 0.02,
                                      "models": ["chosen-model"], "failure": None}


@pytest.mark.parametrize("failure", list(model_call.CallFailure))
def test_every_execution_failure_keeps_its_evidence_and_never_parses(
    tmp_path: Path, failure: model_call.CallFailure,
) -> None:
    execution = model_call.Execution(None, record_id=9, cost_usd=0.01, failure=failure,
                                     reason="the recorded reason", models=("chosen-model",))
    booked: list[model_call.Execution] = []

    async def book(done: model_call.Execution) -> None:
        booked.append(done)

    def parse(text: str) -> None:
        pytest.fail("a failed execution must not be parsed")

    done = asyncio.run(model_call.call(_request(tmp_path, on_result=book), parse=parse,
                                      backend=_Backend(execution)))
    assert (done.value, done.failure, done.reason) == (None, failure, "the recorded reason")
    assert booked == [execution] and done.execution is execution
    assert (done.record_id, done.cost_usd) == (9, 0.01)
    assert done.execution.round()["failure"] == failure.value


@pytest.mark.parametrize("error", [ValueError("invalid verdict"), RuntimeError("parser failed")])
def test_parse_errors_keep_the_completed_runs_cost_and_original_exception(
    tmp_path: Path, error: Exception,
) -> None:
    execution = model_call.Execution("unreadable", record_id=11, cost_usd=0.03)

    def parse(text: str) -> None:
        raise error

    done = asyncio.run(model_call.call(_request(tmp_path), parse=parse, backend=_Backend(execution)))
    assert done.failure is model_call.CallFailure.PARSE and done.value is None
    assert done.parse_error is error and done.reason == str(error)
    assert (done.record_id, done.cost_usd) == (11, 0.03)
    assert done.execution.failure is None and done.execution.round()["failure"] is None


@pytest.mark.parametrize("stage", ["backend", "booking", "parse"])
def test_cancellation_is_never_a_failed_verdict(tmp_path: Path, stage: str) -> None:
    class Backend(_Backend):
        async def run(self, request: Any) -> model_call.Execution:
            if stage == "backend":
                raise asyncio.CancelledError
            return self.execution

    async def book(done: model_call.Execution) -> None:
        if stage == "booking":
            raise asyncio.CancelledError

    def parse(text: str) -> str:
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(model_call.call(_request(tmp_path, on_result=book), parse=parse,
                                    backend=Backend(model_call.Execution("answer"))))


@pytest.mark.parametrize(("failure", "expected"), [
    (None, None), ("refused", "unavailable"), ("timeout", "timeout"),
    ("budget", "budget"), ("failed", "runtime"),
])
def test_a_processor_recipe_keeps_its_private_record_inputs_and_maps_only_its_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, failure: str | None, expected: str | None,
) -> None:
    from looper.agents import processor

    profile = {"profile_id": "chosen", "model": {"name": "chosen-model"}}
    seen: list[dict[str, Any]] = []

    async def run(**kwargs: Any) -> processor.ProcessorRun:
        seen.append(kwargs)
        return processor.ProcessorRun("42" if failure is None else None, record_id=13, cost_usd=0.04,
                                       failure=processor.ProcessorFailure(failure) if failure else None,
                                       detail="" if failure is None else "original reason", profile=profile)

    monkeypatch.setattr(processor, "run", run)
    kwargs = dict(owner="check-owner", instructions="Check the item.", input_text="private input",
                  input_ref={"window": 1}, quiet_word="NONE", budget_usd=0.03, timeout_s=12,
                  runtime_root=tmp_path, claude_bin="chosen-claude", metadata={"check": {"items": 1}},
                  contract="JSON only", role=AgentType.LEADS_CHECK)
    done = asyncio.run(model_call.call(model_call.ProcessorCall(**kwargs), parse=int))
    assert seen == [kwargs] and done.execution.profile is profile
    assert (done.failure.value if done.failure is not None else None) == expected
    assert (done.record_id, done.cost_usd, done.model) == (13, 0.04, "chosen-model")
    assert (done.value, done.reason) == ((42, "") if failure is None else (None, "original reason"))


_CLAUDE = r'''
import json, pathlib, sys, time
pathlib.Path(SEEN).write_text(sys.stdin.readline())
model = sys.argv[sys.argv.index("--model") + 1]
model = {"opus": "claude-opus-5-5"}.get(model, model)
def emit(event):
    print(json.dumps(event), flush=True)
emit({"type": "system", "subtype": "init", "session_id": "call-session", "model": model,
      "tools": [], "mcp_servers": []})
if MODE == "hang":
    time.sleep(60)
failed = MODE in ("runtime", "rate_limited")
emit({"type": "result", "subtype": "error_during_execution" if failed else "success",
      "is_error": failed, "result": '{"answer":42}', "session_id": "call-session",
      "stop_reason": "refusal" if MODE == "refused" else "end_turn",
      "total_cost_usd": 0.5 if MODE == "budget" else 0.02,
      "modelUsage": {model: {"costUSD": 0.5 if MODE == "budget" else 0.02}},
      **({"api_error_status": 429} if MODE == "rate_limited" else {})})
'''


@pytest.mark.parametrize(("mode", "failure"), [
    ("answer", None), ("rate_limited", "rate_limited"), ("refused", "refused"),
    ("runtime", "runtime"), ("budget", "budget"),
])
def test_the_subscription_backend_maps_real_one_shot_results(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, mode: str, failure: str | None,
) -> None:
    seen = tmp_path / "stdin.json"
    executable = fake_claude(tmp_path / "claude", f"MODE = {mode!r}\nSEEN = {str(seen)!r}\n" + _CLAUDE)

    async def test(pool: Any) -> None:
        done = await model_call.call(_request(tmp_path, claude_bin=executable), parse=json.loads)
        assert (done.failure.value if done.failure is not None else None) == failure
        assert done.record_id is not None and done.cost_usd is not None
        assert seen.exists() and json.loads(seen.read_text())["message"]["content"] == "What is six times seven?"
        row = await pool.fetchrow("SELECT status, cost_usd FROM llm_records WHERE id = $1", done.record_id)
        assert row is not None and float(row["cost_usd"]) == done.cost_usd
        if failure is None:
            assert done.value == {"answer": 42} and row["status"] == "completed"
        else:
            assert done.value is None and done.reason

    run_db(test, monkeypatch)


@pytest.mark.parametrize(("mode", "failure"), [("under_minimum", "budget"), ("unavailable", "unavailable")])
def test_a_call_that_cannot_start_writes_no_run_or_process_input(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, mode: str, failure: str,
) -> None:
    seen = tmp_path / "stdin.json"
    executable = fake_claude(tmp_path / "claude", f"MODE = 'answer'\nSEEN = {str(seen)!r}\n" + _CLAUDE)

    async def test(pool: Any) -> None:
        request = _request(tmp_path, claude_bin=executable)
        request = (replace(request, cap_usd=0.05) if mode == "under_minimum"
                   else replace(request, settings=replace(request.settings, error="settings unavailable")))
        done = await model_call.call(request, parse=json.loads)
        assert done.failure.value == failure and done.record_id is None and done.value is None
        assert not seen.exists()
        assert await pool.fetchval("SELECT count(*) FROM llm_records") == 0

    run_db(test, monkeypatch)


def test_the_deadline_ends_a_run_while_its_reply_is_being_read(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    seen = tmp_path / "stdin.json"
    executable = fake_claude(tmp_path / "claude", f"MODE = 'hang'\nSEEN = {str(seen)!r}\n" + _CLAUDE)

    async def test(pool: Any) -> None:
        request = _request(tmp_path, claude_bin=executable)
        request = replace(request, child=replace(request.child, timeout_s=2))
        done = await model_call.call(request, parse=json.loads)
        assert done.failure is model_call.CallFailure.TIMEOUT and done.value is None
        assert done.record_id is not None and seen.exists()
        events = await pool.fetch("SELECT event FROM llm_record_events WHERE record_id = $1", done.record_id)
        assert any(json.loads(row["event"]).get("subtype") == "init" for row in events)
        row = await pool.fetchrow("SELECT status, error FROM llm_records WHERE id = $1", done.record_id)
        assert row["status"] == "failed" and "ran over 2 s" in row["error"]

    run_db(test, monkeypatch, timeout=30)
