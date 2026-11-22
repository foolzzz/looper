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


def test_a_success_can_contain_none_without_becoming_a_failure(tmp_path: Path) -> None:
    done = asyncio.run(model_call.call(_request(tmp_path), parse=lambda _: None,
                                      backend=_Backend(model_call.Execution("null"))))
    assert isinstance(done, model_call.CallOk) and done.value is None


@pytest.mark.parametrize("caller", ["outbound", "advisor", "judge", "leads_check"])
@pytest.mark.parametrize("failure", list(model_call.CallFailure))
def test_every_caller_handles_every_call_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caller: str, failure: model_call.CallFailure,
) -> None:
    from looper.agents import advisor, extraction, leads_check, outbound
    from looper.agents.advice import AdviceKind, AdviceRequest
    from looper.agents.serve import seed_role_configs
    from looper.db import agent_spawns
    from looper.db.agent_spawns import SpawnState

    async def run(self: Any, request: Any) -> model_call.Execution:
        return model_call.Execution(None, failure=failure, reason="recorded reason")

    monkeypatch.setattr(model_call.SubscriptionBackend, "run", run)

    async def test(pool: Any) -> None:
        runtime = {"runtime_root": tmp_path, "claude_bin": "unused-model-cli"}
        if caller == "outbound":
            result = await outbound.review(
                outbound.Action("looper_lark", outbound.ActionLevel.EXTERNAL, "chat", "Build status",
                                "Send build status", action="send", content="The build is green."),
                outbound.Asker(outbound.Who.OPERATOR, "lark", "Post the build status."),
                outbound.Origin("manager", outbound.Source.TOOL), **runtime)
            expected = "runtime" if failure is model_call.CallFailure.BLOCKED else failure.value
            assert result.decision is outbound.Decision.BLOCK and result.failure.value == expected
            row = await pool.fetchrow("SELECT failure FROM outbound_reviews WHERE id = $1", result.review_id)
            assert row["failure"] == expected
        elif caller == "leads_check":
            result = await leads_check.check(
                "leads", [], messages={}, participants=extraction.Participants({}, "ou_synthetic"),
                zone_name="UTC", text_max_chars=500, types={}, owner="failure-check", reference={},
                budget_usd=0.5, timeout_s=20, **runtime)
            expected = {"unavailable": "refused", "refused": "refused", "timeout": "timeout", "budget": "budget",
                        "blocked": "blocked"}
            assert result.checked is None and result.counts["check_failed"] == 1
            assert result.detail == f"{expected.get(failure.value, 'failed')}: recorded reason"
        else:
            await pool.execute("INSERT INTO projects (id, name) VALUES ('project-failure', 'Failure checks')")
            await pool.execute("INSERT INTO tasks (id, project_id, requirement, status) "
                               "VALUES ('task-failure', 'project-failure', 'Improve reads.', 'running')")
            brief = advisor.Brief("task-failure", None, 1, advisor.Source.OPTIONAL)
            row = await agent_spawns.create(
                kind="advisor", task_id=brief.task_id, step_id=None, parent_record_id=None, brief=brief.row(),
                inputs={}, budget_usd=1.0, state=SpawnState.ASKING if caller == "advisor" else SpawnState.LINTING)
            request = AdviceRequest(AdviceKind.DIAGNOSE, {"problem": "Slow reads"})
            if caller == "advisor":
                result = await advisor._ask(row, brief, request, timeout_s=20, **runtime)
                expected = failure.value
            else:
                result = await advisor._judge(row, brief, request, **runtime)
                expected = "rate_limited" if failure is model_call.CallFailure.RATE_LIMITED else "judge"
            assert isinstance(result, advisor.Ended) and result.failure == expected
            assert result.state is (SpawnState.OVER_BUDGET if failure is model_call.CallFailure.BUDGET
                                    else SpawnState.FAILED)
            saved = await agent_spawns.get(row["id"])
            entry = saved["result"]["rounds" if caller == "advisor" else "judges"][-1]
            assert entry["failure"] == failure.value

    run_db(test, monkeypatch, setup=seed_role_configs)


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
    assert isinstance(done, model_call.CallOk) and done.value == 42
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
    assert isinstance(done, model_call.CallFailed)
    assert (done.failure, done.reason, done.error) == (failure, "the recorded reason", None)
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
    assert isinstance(done, model_call.CallFailed) and done.failure is model_call.CallFailure.PARSE
    assert done.error is error and done.reason == str(error)
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
    ("budget", "budget"), ("failed", "runtime"), ("blocked", "blocked"),
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
                  contract="JSON only", role=AgentType.LEADS_CHECK,
                  project_id="project-synthetic", schedule_id="schedule-synthetic")
    done = asyncio.run(model_call.call(model_call.ProcessorCall(**kwargs), parse=int))
    assert seen == [kwargs] and done.execution.profile is profile
    assert (done.failure.value if isinstance(done, model_call.CallFailed) else None) == expected
    assert (done.record_id, done.cost_usd, done.model) == (13, 0.04, "chosen-model")
    if failure is None:
        assert isinstance(done, model_call.CallOk) and done.value == 42
    else:
        assert isinstance(done, model_call.CallFailed) and done.reason == "original reason"


@pytest.mark.parametrize("failure", [None, "refused", "timeout", "budget", "failed", "blocked"])
def test_processor_call_retains_the_processors_result_contract(tmp_path: Path, failure: str | None) -> None:
    from looper.agents import processor

    original = processor.ProcessorRun("" if failure is None else None, 13, 0.04,
        failure=processor.ProcessorFailure(failure) if failure else None, detail="original detail",
        profile={"profile_id": "chosen"})
    mapped = {"refused": "unavailable", "failed": "runtime"}
    execution = model_call.Execution(original.reply, original.record_id, original.cost_usd,
        model_call.CallFailure(mapped.get(failure, failure)) if failure else None,
        original.detail, profile=original.profile)
    result = asyncio.run(model_call.call(_request(tmp_path), parse=str, backend=_Backend(execution)))
    assert model_call.processor_result(result) == original


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
        assert (done.failure.value if isinstance(done, model_call.CallFailed) else None) == failure
        assert done.record_id is not None and done.cost_usd is not None
        assert seen.exists() and json.loads(seen.read_text())["message"]["content"] == "What is six times seven?"
        row = await pool.fetchrow("SELECT status, cost_usd FROM llm_records WHERE id = $1", done.record_id)
        assert row is not None and float(row["cost_usd"]) == done.cost_usd
        if failure is None:
            assert isinstance(done, model_call.CallOk) and done.value == {"answer": 42}
            assert row["status"] == "completed"
        else:
            assert isinstance(done, model_call.CallFailed) and done.reason

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
        assert isinstance(done, model_call.CallFailed) and done.failure.value == failure and done.record_id is None
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
        assert isinstance(done, model_call.CallFailed) and done.failure is model_call.CallFailure.TIMEOUT
        assert done.record_id is not None and seen.exists()
        events = await pool.fetch("SELECT event FROM llm_record_events WHERE record_id = $1", done.record_id)
        assert any(json.loads(row["event"]).get("subtype") == "init" for row in events)
        row = await pool.fetchrow("SELECT status, error FROM llm_records WHERE id = $1", done.record_id)
        assert row["status"] == "failed" and "ran over 2 s" in row["error"]

    run_db(test, monkeypatch, timeout=30)
