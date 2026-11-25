"""Failure classes of runs (``looper.agents.failures``), without a database.

The Claude Code and Codex exec samples under ``fixtures/failures`` are recorded run streams with their ids, times and
texts replaced; the Codex app-server ones are JSON-RPC notifications shaped by codex-cli 0.169.0's generated protocol
schema (no recorded app-server failure exists yet).
"""

from __future__ import annotations

import asyncio
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from looper.agents import failures
from looper.agents.failures import Evidence, Failure, FailureCause, FailureClass, Signal
from looper.agents.runtimes import ADAPTERS

FIXTURES = Path(__file__).parent / "fixtures" / "failures"
RESET = datetime.fromtimestamp(1800000000, tz=timezone.utc)


def _stream(name: str) -> tuple[dict[str, Any] | None, Evidence]:
    """A sample's result and the evidence its other events give, as the recorder gathers them."""
    evidence, result = Evidence(), None
    for line in (FIXTURES / name).read_text().splitlines():
        event = json.loads(line)
        evidence.observe(event)
        if event.get("type") == "result":
            result = event
    return result, evidence


def _app_server(name: str) -> tuple[dict[str, Any] | None, Evidence]:
    """A Codex app-server sample's notifications, translated as a Worker's app-server run translates them."""
    from looper.agents.runtimes.codex import CodexStream
    from looper.agents.runtimes.codex_steer import exec_events

    stream, evidence, result = CodexStream(model="gpt-test"), Evidence(), None
    for line in (FIXTURES / name).read_text().splitlines():
        message = json.loads(line)
        for raw in exec_events(message["method"], message["params"]):
            for event in stream.translate(raw):
                evidence.observe(event)
                if event.get("type") == "result":
                    result = event
    return result, evidence


def _classify(runtime: str, result: dict[str, Any] | None, evidence: Evidence, **kwargs: Any) -> Failure:
    return failures.classify(ADAPTERS[runtime], result, evidence, **kwargs)


@pytest.mark.parametrize("name,runtime,cause,signal,resets_at", [
    ("claude_usage_limit.jsonl", "claude-code", FailureCause.USAGE_LIMIT, Signal.TYPED, RESET),
    ("claude_usage_limit_older.jsonl", "claude-code", FailureCause.USAGE_LIMIT, Signal.TYPED, RESET),
    ("claude_connection_failed.jsonl", "claude-code", FailureCause.CONNECTION_FAILED, Signal.TYPED, None),
    ("codex_exec_capacity.jsonl", "codex", FailureCause.PROVIDER_OVERLOADED, Signal.TEXT, None),
    ("codex_exec_reconnect.jsonl", "codex", FailureCause.CONNECTION_FAILED, Signal.TEXT, None),
])
def test_recorded_failures_get_their_cause(name, runtime, cause, signal, resets_at):
    result, evidence = _stream(name)
    assert _classify(runtime, result, evidence) == Failure(cause, signal, resets_at)


@pytest.mark.parametrize("name,cause", [
    ("codex_app_server_usage_limit.jsonl", FailureCause.USAGE_LIMIT),
    # The retried disconnection says nothing; the final one, with a 502, is the provider's.
    ("codex_app_server_stream_disconnected.jsonl", FailureCause.PROVIDER_OVERLOADED),
])
def test_codex_app_server_error_info_is_typed(name, cause):
    result, evidence = _app_server(name)
    assert _classify("codex", result, evidence) == Failure(cause, Signal.TYPED)


def test_codex_error_info_reaches_the_failed_result_and_the_error_event():
    from looper.agents.runtimes.codex import CodexStream
    from looper.agents.runtimes.codex_steer import exec_events

    failed = exec_events("turn/completed", {"turn": {"status": "failed", "error": {
        "message": "limit", "codexErrorInfo": "usageLimitExceeded"}}})
    assert failed == [{"type": "turn.failed", "error": {"message": "limit", "codex_error_info": "usageLimitExceeded"}}]
    result = CodexStream(model="gpt-test").translate(failed[0])[0]
    assert result["codex_error_info"] == "usageLimitExceeded" and result["errors"] == ["limit"]
    error = exec_events("error", {"willRetry": True, "error": {"message": "again", "codexErrorInfo": "serverOverloaded"}})
    assert error == [{"type": "error", "message": "again", "codex_error_info": "serverOverloaded", "will_retry": True}]
    system = CodexStream(model="gpt-test").translate(error[0])[0]
    assert (system["codex_error_info"], system["will_retry"]) == ("serverOverloaded", True)
    evidence = Evidence()
    evidence.observe(system)
    assert evidence.codex_error_info is None  # a retried error is not the run's failure


def test_codex_events_without_error_info_are_unchanged():
    """Codex exec, and app-server errors without codexErrorInfo, translate exactly as before."""
    from looper.agents.runtimes.codex import CodexStream
    from looper.agents.runtimes.codex_steer import exec_events

    assert exec_events("error", {"error": {"message": "boom"}}) == [{"type": "error", "message": "boom"}]
    assert exec_events("error", {"error": "not an object"}) == [{"type": "error", "message": None}]
    assert exec_events("turn/completed", {"turn": {"status": "failed", "error": {"message": "boom"}}}) == [
        {"type": "turn.failed", "error": {"message": "boom"}}]
    assert exec_events("turn/completed", {"turn": {"status": "interrupted"}}) == [
        {"type": "turn.failed", "error": {"message": "the turn was interrupted"}}]
    stream = CodexStream(model="gpt-test")
    assert set(stream.translate({"type": "error", "message": "boom"})[0]) == {"type", "subtype", "message",
                                                                                  *stream._base()}
    assert "codex_error_info" not in stream.translate({"type": "turn.failed", "error": {"message": "boom"}})[0]


@pytest.mark.parametrize("info,cause", [
    ("usageLimitExceeded", FailureCause.USAGE_LIMIT), ("rateLimitExceeded", FailureCause.RATE_LIMIT),
    ("serverOverloaded", FailureCause.PROVIDER_OVERLOADED), ("internalServerError", FailureCause.PROVIDER_OVERLOADED),
    ("unauthorized", FailureCause.LOGIN), ("contextWindowExceeded", FailureCause.CONTEXT),
    ("sessionBudgetExceeded", FailureCause.BUDGET),
    ({"httpConnectionFailed": {"httpStatusCode": None}}, FailureCause.CONNECTION_FAILED),
    ({"responseStreamConnectionFailed": {"httpStatusCode": 429}}, FailureCause.RATE_LIMIT),
    ({"responseStreamDisconnected": {"httpStatusCode": 503}}, FailureCause.PROVIDER_OVERLOADED),
    ("badRequest", FailureCause.UNKNOWN), ({"somethingNew": {}}, FailureCause.UNKNOWN),
])
def test_codex_error_info_values(info, cause):
    result = {"type": "result", "is_error": True, "subtype": "error_during_execution", "errors": ["x"],
              "codex_error_info": info}
    assert _classify("codex", result, Evidence()).cause is cause


@pytest.mark.parametrize("text,cause", [
    # codex-cli 0.169.0's messages for these errors.
    ("You've hit your usage limit. Upgrade to Pro to continue.", FailureCause.USAGE_LIMIT),
    ("Quota exceeded. Check your plan and billing details.", FailureCause.USAGE_LIMIT),
    ("exceeded retry limit, last status: 429 Too Many Requests", FailureCause.RATE_LIMIT),
    ("exceeded retry limit, last status: 502 Bad Gateway", FailureCause.PROVIDER_OVERLOADED),
    ("stream disconnected before completion: error sending request", FailureCause.CONNECTION_FAILED),
    ("Codex ran out of room in the model's context window. Start a new thread.", FailureCause.CONTEXT),
    ("the turn failed", FailureCause.UNKNOWN),
])
def test_codex_exec_texts(text, cause):
    result = {"type": "result", "is_error": True, "subtype": "error_during_execution", "errors": [text]}
    found = _classify("codex", result, Evidence())
    assert found.cause is cause
    assert found.signal is (Signal.TYPED if cause is FailureCause.UNKNOWN else Signal.TEXT)


@pytest.mark.parametrize("fields,cause", [
    ({"api_error_status": 429, "api_error_type": "rate_limit_error"}, FailureCause.RATE_LIMIT),
    ({"api_error_status": 529, "api_error_type": "overloaded_error"}, FailureCause.PROVIDER_OVERLOADED),
    ({"api_error_status": 500}, FailureCause.PROVIDER_OVERLOADED),
    ({"api_error_status": 401}, FailureCause.LOGIN),
    ({"api_error_type": "authentication_error"}, FailureCause.LOGIN),
    ({"result": "Prompt is too long"}, FailureCause.CONTEXT),
    ({"result": "something else went wrong"}, FailureCause.UNKNOWN),
])
def test_claude_code_typed_fields_and_texts(fields, cause):
    result = {"type": "result", "subtype": "success", "is_error": True, "result": "", **fields}
    assert _classify("claude-code", result, Evidence()).cause is cause


@pytest.mark.parametrize("kind,status,cause", [
    ("rate_limited", 429, FailureCause.RATE_LIMIT), ("auth", 401, FailureCause.LOGIN),
    ("context_overflow", 400, FailureCause.CONTEXT), ("not_found", 404, FailureCause.PROFILE),
    ("unavailable", 503, FailureCause.PROVIDER_OVERLOADED), ("unavailable", None, FailureCause.CONNECTION_FAILED),
    ("protocol", None, FailureCause.UNKNOWN), (None, None, FailureCause.UNKNOWN),
])
def test_api_runtime_error_kinds(kind, status, cause):
    result = {"type": "result", "subtype": "error_during_execution", "is_error": True, "errors": ["x"],
              "error_kind": kind, "error_status": status}
    assert _classify("api", result, Evidence()).cause is cause


def test_a_successful_result_has_no_runtime_signal():
    success = {"type": "result", "subtype": "success", "is_error": False, "result": "done"}
    _, evidence = _stream("claude_usage_limit.jsonl")  # even after a rejected window, a success is a success
    for runtime in ("claude-code", "codex", "api"):
        assert ADAPTERS[runtime].failure_signal(success, evidence) is None
    # Its own report can still say the step failed.
    assert _classify("claude-code", success, Evidence(), outcome="reported_failed") == Failure(FailureCause.REPORT)


def test_looper_s_own_reasons_come_first_then_the_runtime_s_then_the_report_then_the_cap():
    result, evidence = _stream("claude_usage_limit.jsonl")
    assert _classify("claude-code", result, evidence, given=failures.TIMED_OUT) == Failure(FailureCause.TIMEOUT)
    assert _classify("claude-code", result, evidence, outcome="contract").cause is FailureCause.USAGE_LIMIT
    capped = {"type": "result", "subtype": "error_max_budget_usd", "is_error": True, "errors": []}
    assert _classify("claude-code", capped, Evidence()).cause is FailureCause.BUDGET
    assert _classify("claude-code", None, Evidence()) == Failure(FailureCause.UNKNOWN)
    assert failures.classify(None, None, Evidence(), outcome="files_mismatch") == Failure(FailureCause.REPORT)


def test_a_broken_runtime_signal_is_unknown(caplog):
    class Broken:
        type = "broken"

        def failure_signal(self, result, evidence):
            raise RuntimeError("bug")

        def budget_error(self, result):
            return None

    found = failures.classify(Broken(), {"type": "result", "is_error": True}, Evidence())
    assert found == Failure(FailureCause.UNKNOWN)
    assert "could not classify a broken run's failure" in caplog.text


def test_start_failures():
    from looper.agents.runtimes import RuntimeUnsupported
    from looper.agents.runtimes.channel import RunStartError, RunStartTimeout

    def raised(exc, cause=None):
        try:
            try:
                raise cause or ValueError()
            except BaseException as inner:
                raise exc from inner
        except BaseException as outer:
            return failures.start_failure(outer)

    assert raised(RunStartTimeout("keeper did not open its control socket within 10 s")).cause \
        is FailureCause.START_TIMEOUT
    assert raised(RunStartError("x"), FileNotFoundError(2, "No such file")).cause is FailureCause.RUNTIME_MISSING
    assert failures.start_failure(FileNotFoundError(2, "No such file")).cause is FailureCause.RUNTIME_MISSING
    assert failures.start_failure(RuntimeUnsupported("no")).cause is FailureCause.PROFILE
    assert raised(RunStartError("keeper launch refused: control plane is fenced")).cause is FailureCause.UNKNOWN


def test_every_cause_has_one_class_and_the_schema_check_lists_them():
    from looper.db import connection

    assert set(failures.CAUSE_CLASS) == set(FailureCause)
    assert set(failures.CAUSE_CLASS.values()) == set(FailureClass)
    schema = " ".join(connection.SCHEMA_PATH.read_text().split())
    assert f"CONSTRAINT llm_records_failure_check CHECK ({failures.check_sql()})" in schema


def test_failure_metadata():
    assert Failure(FailureCause.USAGE_LIMIT, resets_at=RESET).metadata() == {
        "signal": "typed", "resets_at": "2027-01-15T08:00:00+00:00"}
    assert Failure(FailureCause.CONNECTION_FAILED, Signal.TEXT).metadata() == {"signal": "text"}
    assert Failure(FailureCause.USAGE_LIMIT).cls is FailureClass.QUOTA


@pytest.mark.parametrize("stored,mode", [
    ({}, failures.FailureTyping.OFF), ({"engine.failure_typing": "record"}, failures.FailureTyping.RECORD),
    ({"engine.failure_typing": "off"}, failures.FailureTyping.OFF),
    ({"engine.failure_typing": "everything"}, failures.FailureTyping.OFF), (RuntimeError("down"), failures.FailureTyping.OFF),
])
def test_typing_mode(monkeypatch, stored, mode):
    from looper.db import system_settings

    requested = []

    async def get_settings(keys, *, pool=None):
        requested.append(keys)
        if isinstance(stored, Exception):
            raise stored
        return stored

    monkeypatch.setattr(system_settings, "get_settings", get_settings)
    assert asyncio.run(failures.typing_mode()) is mode
    assert requested == [["engine.failure_typing"]]


def test_evidence_keeps_only_bounded_facts():
    evidence = Evidence()
    for n in range(20):
        evidence.observe({"type": "system", "subtype": "codex_error", "message": f"m{n}"})
    evidence.observe({"type": "rate_limit_event", "rate_limit_info": {"status": "allowed", "resetsAt": 1}})
    evidence.observe({"type": "assistant", "message": {"content": []}})
    assert evidence.codex_messages == [f"m{n}" for n in range(15, 20)]
    assert evidence.rejected is None and evidence.assistant_error is None
    assert re.fullmatch(r"\{.*\}", json.dumps(vars(evidence)))  # plain data, nothing from the model's messages
