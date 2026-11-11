"""Fallback detection uses model events; legacy prompt bytes stay unchanged."""

from hashlib import sha256

import pytest

from looper.agents.agent_settings import BUILTIN_SETTINGS
from looper.agents.runtimes import ADAPTERS
from looper.agents.system_prompt import REVIEW_CONTRACT, WORK_CONTRACT, build_system_prompt
from looper.agents.tiers.planner import DEFAULT_SYSTEM_PROMPT as PLANNER_PROMPT
from looper.agents.tiers.planner_protocol import REPLY_CONTRACT
from looper.agents.tiers.worker import DEFAULT_SYSTEM_PROMPT as WORKER_PROMPT, REVIEWER_SYSTEM_PROMPT


@pytest.mark.parametrize(("event", "expected"), [
    ({"type": "system", "subtype": "init", "model": "example-model"}, True),
    ({"type": "system", "subtype": "init", "session_id": "session-1"}, False),
    ({"type": "assistant", "message": {"content": [{"type": "text", "text": "Ready"}]}}, True),
    ({"type": "assistant", "message": {"model": "example-model"}}, True),
    ({"type": "assistant", "message": {"content": []}}, False),
    ({"type": "assistant", "message": {"model": True, "content": "Unavailable"}}, False),
    ({"type": "assistant", "is_error": True,
      "message": {"content": [{"type": "text", "text": "Unavailable"}]}}, False),
    ({"type": "assistant", "message": {"model": "<synthetic>", "content": [{"type": "text", "text": "Error"}]}}, False),
    ({"type": "system", "subtype": "status", "model": "example-model"}, False),
    ({"type": "result", "modelUsage": {"example-model": {"inputTokens": 0}}}, False),
    ({"type": "result", "is_error": True, "modelUsage": {"example-model": {"inputTokens": 1}}}, True),
])
def test_only_model_events_prevent_pre_response_fallback(event: dict, expected: bool) -> None:
    assert ADAPTERS["claude-code"].is_model_event(event) is expected


def test_unverified_runtime_first_event_detection_stays_disabled() -> None:
    assert all(not adapter.first_event_known for adapter in ADAPTERS.values())


@pytest.mark.parametrize(("runtime", "event", "expected"), [
    ("claude-code", {"type": "system", "subtype": "init", "model": "reported-model"}, "reported-model"),
    ("claude-code", {"type": "assistant", "message": {"model": "reported-model"}}, "reported-model"),
    ("claude-code", {"type": "assistant", "message": {"model": "<synthetic>"}}, None),
    ("claude-code", {"type": "assistant", "parent_tool_use_id": "call-1",
                     "message": {"model": "subagent-model"}}, None),
    ("cursor-agent", {"type": "system", "subtype": "init", "model": "requested-model",
                      "model_display": "Reported model"}, "Reported model"),
    ("cursor-agent", {"type": "system", "subtype": "init", "model": "requested-model"}, None),
    ("codex", {"type": "assistant", "message": {"model": "requested-model"}}, None),
    ("grok", {"type": "system", "subtype": "init", "model": "requested-model"}, None),
    *[(runtime, {"type": "system", "subtype": "init", "model": "requested-model"}, None)
      for runtime in ("pi", "omp", "dsh")],
    *[(runtime, {"type": "assistant", "message": {"model": "reported-model"}}, "reported-model")
      for runtime in ("pi", "omp", "grok")],
    ("grok", {"type": "assistant", "message": {"model": "unknown"}}, None),
    ("grok", {"type": "assistant", "parent_tool_use_id": "child", "message": {"model": "child-model"}}, None),
])
def test_reported_model_never_uses_normalized_requested_model(runtime: str, event: dict, expected: str | None) -> None:
    assert ADAPTERS[runtime].reported_model(event) == expected


@pytest.mark.parametrize("runtime", ["pi", "omp", "dsh", "grok"])
@pytest.mark.parametrize("reported", [None, "reported-model"])
def test_normalized_events_preserve_only_native_model_reports(runtime, reported):
    from looper.agents.runtimes.pi import PiStream
    from looper.agents.runtimes.omp import OmpStream
    from looper.agents.runtimes.dsh import DshStream
    from looper.agents.runtimes.grok import GrokStream

    stream = {"pi": PiStream, "omp": OmpStream, "dsh": DshStream, "grok": GrokStream}[runtime](model="configured")
    message = {"role": "assistant", "content": [{"type": "text", "text": "Ready"}],
               **({"model": reported} if reported else {})}
    raw = ({"type": "text", "text": "Ready"} if runtime == "dsh" else
           {"type": "assistant" if runtime == "grok" else "message_end", "message": message})
    events = stream.translate(raw)
    assistant = next(event for event in events if event["type"] == "assistant")
    assert ADAPTERS[runtime].reported_model(assistant) == (None if runtime == "dsh" else reported)


def test_grok_acp_assistant_does_not_report_the_configured_model():
    from looper.agents.runtimes.grok_acp import AcpTurn

    turn = AcpTurn(model="configured", session_id="test-session")
    turn.feed({"method": "session/update", "params": {"update": {
        "sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "Ready"}}}})
    events = turn.finish({"result": {"stopReason": "end_turn"}}, duration_ms=1)
    assistant = next(event for event in events if event["type"] == "assistant")
    assert ADAPTERS["grok"].reported_model(assistant) is None


@pytest.mark.parametrize(("role", "text", "contract", "prompt_hash", "contract_hash"), [
    ("planner", PLANNER_PROMPT, REPLY_CONTRACT,
     "ff9db55ffd76cb47c881cd261e215a82545587d36e3977bb6529ec7f0a824c78",
     "2f72cd02be65030f0d1635b2c276dc6debac28eac9d378040c3aa3d5ef6ddc42"),
    ("worker", WORKER_PROMPT, WORK_CONTRACT,
     "d758330eb7193a80e5e8ce8c02a9ca19b788bd9f60e6a679734a7381efb8e1ed",
     "ac9f8a0bca5ee5a86259d4b4043154e8841de92dc1c20f94e34a1b5a8ecb79e6"),
    ("reviewer", REVIEWER_SYSTEM_PROMPT, REVIEW_CONTRACT,
     "4095f65bdd550d63229ffa4d7c968bd1800887296a4b7178b6e4cb780646b0f0",
     "4605220c239c54b4fc3321f2423720cbea8e6aa4102c6493ebd3f1eabe1ae3e4"),
])
def test_profiles_without_fallback_preserve_prompt_and_contract_bytes(
    role: str, text: str, contract: str, prompt_hash: str, contract_hash: str,
) -> None:
    contract_name = {"planner": "REPLY_CONTRACT", "worker": "WORK_CONTRACT", "reviewer": "REVIEW_CONTRACT"}[role]
    prompt = build_system_prompt(
        harness=BUILTIN_SETTINGS.harness, role_id=role, role=text, recommended=True,
        task_kind=role, task="## Task\nCheck the result.", contract_name=contract_name, contract=contract,
    )
    assert prompt.sha256 == prompt_hash
    assert sha256(contract.encode()).hexdigest() == contract_hash
