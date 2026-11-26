"""Only structured model API failures can start a network pause."""

import pytest

from looper.agents import failures
from looper.agents.failures import Evidence, Failure, FailureCause, Signal
from looper.agents.runtimes import ADAPTERS


@pytest.mark.parametrize("runtime,fields,expected", [
    ("claude-code", {"api_error": "connection_failed"}, True),
    ("codex", {"codex_error_info": {"httpConnectionFailed": {"httpStatusCode": None}}}, True),
    ("api", {"error_kind": "unavailable", "error_status": None}, True),
    ("api", {"error_kind": "unavailable", "error_status": 503}, False),
    ("claude-code", {"api_error_code": "ENOTFOUND"}, True),
    ("claude-code", {"api_error": {"code": "ECONNRESET"}}, True),
    ("claude-code", {"error_source": "model_api", "error_code": "EAI_AGAIN"}, True),
    ("claude-code", {"error_source": "model_api", "exit_reason": "ECONNREFUSED"}, True),
    ("claude-code", {"error_source": "model_api", "code": "ETIMEDOUT"}, False),
    ("claude-code", {"error_source": "tool", "error_code": "ENOTFOUND"}, False),
    ("claude-code", {"error_code": "ECONNREFUSED"}, False),
    ("claude-code", {"result": "Can't reach the API server (ENOTFOUND)"}, False),
    ("codex", {"errors": ["stream disconnected before completion: error sending request"]}, False),
    ("claude-code", {"api_error_type": "authentication_error"}, False),
    ("claude-code", {"api_error_status": 429}, False),
    ("claude-code", {"api_error_code": "ENOTFOUND", "api_error_status": 401}, False),
])
def test_network_classification_requires_a_model_api_signal(runtime, fields, expected):
    result = {"type": "result", "is_error": True, "subtype": "error_during_execution", **fields}
    assert failures.network_unavailable(ADAPTERS[runtime], result, Evidence()) is expected


@pytest.mark.parametrize("event,expected", [
    ({"type": "assistant", "error": "api_error", "api_error_code": "ENOTFOUND"}, True),
    ({"type": "system", "subtype": "api_error", "api_error": {"code": "ECONNRESET"}}, True),
    ({"type": "tool_result", "api_error_code": "ENOTFOUND"}, False),
    ({"type": "assistant", "message": {"content": [{"text": "ENOTFOUND"}]}}, False),
    ({"type": "system", "subtype": "api_retry", "message": "ENOTFOUND"}, False),
])
def test_stream_evidence_ignores_tool_results_and_free_text(event, expected):
    evidence = Evidence()
    evidence.observe(event)
    result = {"type": "result", "is_error": True, "subtype": "error_during_execution"}
    assert failures.network_unavailable(ADAPTERS["claude-code"], result, evidence) is expected


@pytest.mark.parametrize("later", [
    {"type": "assistant", "message": {"content": [{"type": "text", "text": "Recovered"}]}},
    {"type": "user", "message": {"content": [{"type": "tool_result", "content": "Done"}]}},
    {"type": "tool_result", "content": "Done"},
    {"type": "assistant", "error": "api_error", "api_error_code": "EINVAL"},
    {"type": "system", "subtype": "api_error", "api_error": {"code": "EINVAL"}},
    {"type": "result", "is_error": True, "api_error_code": "EINVAL"},
    {"type": "result", "is_error": True, "error_kind": "other"},
    {"type": "result", "subtype": "error_max_turns", "is_error": True},
    {"type": "result", "subtype": "success", "is_error": False},
])
def test_recovered_or_superseded_transport_error_does_not_pause(later):
    evidence = Evidence()
    evidence.observe({"type": "system", "subtype": "api_error", "api_error": {"code": "ECONNRESET"}})
    evidence.observe(later)
    result = {"type": "result", "is_error": True, "subtype": "error_during_execution",
              "errors": ["Unrelated execution failure"]}
    evidence.observe(result)
    assert failures.network_unavailable(ADAPTERS["claude-code"], result, evidence) is False


@pytest.mark.parametrize("tail", [
    [],
    [{"type": "system", "subtype": "api_retry"}],
    [{"type": "system", "subtype": "status", "status": "idle"}],
    [{"type": "user", "message": {"content": "Continue"}}],
])
def test_terminal_transport_error_survives_bookkeeping_and_generic_result(tail):
    evidence = Evidence()
    evidence.observe({"type": "assistant", "message": {"content": [{"type": "text", "text": "Progress"}]}})
    evidence.observe({"type": "assistant", "error": "api_error", "api_error_code": "ECONNRESET"})
    for event in tail:
        evidence.observe(event)
    result = {"type": "result", "is_error": True, "subtype": "error_during_execution"}
    evidence.observe(result)
    assert failures.network_unavailable(ADAPTERS["claude-code"], result, evidence) is True


def test_success_and_local_failure_override_prior_network_evidence():
    evidence = Evidence()
    evidence.observe({"type": "assistant", "error": "api_error", "api_error_code": "ENOTFOUND"})
    success = {"type": "result", "is_error": False, "subtype": "success"}
    assert failures.network_unavailable(ADAPTERS["claude-code"], success, evidence) is False
    failed = {"type": "result", "is_error": True}
    assert failures.network_unavailable(ADAPTERS["claude-code"], failed, evidence,
                                        given=failures.TIMED_OUT) is False
    assert failures.network_unavailable(None, None, Evidence(),
                                        given=Failure(FailureCause.CONNECTION_FAILED)) is True
    assert failures.network_unavailable(None, None, Evidence(),
                                        given=Failure(FailureCause.CONNECTION_FAILED, Signal.TEXT)) is False
