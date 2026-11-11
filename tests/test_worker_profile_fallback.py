"""Profile fallback through real process streams, with disposable stand-in CLIs."""

import asyncio
import json
from dataclasses import replace
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from fake_claude import fake_claude
from looper.agents.agent_settings import AgentSettings
from looper.agents.message import MessageEnvelope, MessageType, worker_address
from looper.agents.profile_registry import (
    Endpoint, ModelSpec, ProviderSpec, ResolvedAgentProfile, ResolvedModel, RuntimeSpec,
)
from looper.agents.runtimes import ADAPTERS, ApiProtocol, AuthKind, Capability, Launcher, VendorType
from looper.agents.tiers import planner, worker
from test_agent_planner_worker import _step_envelope, _worker_row


LAUNCHES = [("claude-code", Launcher.NATIVE), ("grok", Launcher.NATIVE),
            *[(runtime, launcher) for runtime in ("codex", "coco", "cursor-agent") for launcher in Launcher]]
INIT = {"type": "system", "subtype": "init", "model": "unavailable-model", "session_id": "stand-in"}
ASSISTANT = {"type": "assistant", "message": {"role": "assistant", "content": [
    {"type": "text", "text": "Inspecting the project"}]}}
ERROR = "Unknown model\nUse an available model"
SUCCESS = {"type": "result", "subtype": "success", "result": json.dumps({
    "status": "completed", "summary": "Recovered", "changed_files": {}})}


def _settings(runtime, executable, *, launcher=Launcher.NATIVE, fallback=None):
    adapter = ADAPTERS[runtime]
    vendor = {"claude-code": VendorType.CLAUDE_SUBSCRIPTION, "codex": VendorType.CODEX_LOGIN,
              "coco": VendorType.COCO_LOGIN, "cursor-agent": VendorType.CURSOR_LOGIN,
              "grok": VendorType.GROK_LOGIN}[runtime]
    protocol = ApiProtocol.ANTHROPIC_MESSAGES if runtime == "claude-code" else ApiProtocol.OPENAI_RESPONSES
    provider = ProviderSpec("provider", "Stand-in login", vendor, AuthKind.OAUTH, runtime)
    model = ModelSpec("model", provider.id, "unavailable-model", protocol)
    return AgentSettings(profile=ResolvedAgentProfile(
        "primary", "Primary", RuntimeSpec("runtime", runtime, runtime, executable, adapter.capabilities,
                                           launcher=launcher), model.id,
        resolved_model=ResolvedModel(model, provider, Endpoint(provider.id, protocol, "")),
        fallback_profile_id=fallback))


def _launch_lines(runtime):
    if runtime in ("codex", "coco"):
        lines = [{"type": "thread.started", "thread_id": "stand-in"}, {"type": "turn.started"}]
        if runtime == "coco":
            # Recorded Coco startup warnings are item events, but no model has run.
            lines.insert(1, {"type": "item.completed", "item": {"id": "warning", "type": "error",
                                                                 "message": "Ignoring an unknown setting"}})
        return lines
    return [INIT]


@pytest.fixture
def run_primary(agent_db, tmp_path, monkeypatch):
    # All home/login resolution stays inside the test, including launch record cleanup.
    home = tmp_path / "operator"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    for name in ("CODEX_HOME", "TRAE_HOME", "GROK_HOME", "CURSOR_CONFIG_DIR", "CURSOR_DATA_DIR"):
        directory = home / name.lower()
        directory.mkdir()
        monkeypatch.setenv(name, str(directory))
    monkeypatch.delenv("TRAECLI_HOME", raising=False)
    _worker_row(monkeypatch, {"system_prompt": "ROLE", "timeout_seconds": 5, "max_turns": 0})
    records = []
    processes = []
    spawn = worker.proc_tree.spawn

    async def track_process(*args, **kwargs):
        process = await spawn(*args, **kwargs)
        processes.append(process)
        return process

    monkeypatch.setattr(worker.proc_tree, "spawn", track_process)

    async def start(**fields):
        records.append(fields)
        return len(records)

    async def annotate(record_id, metadata):
        records[record_id - 1]["metadata"].update(metadata)

    agent_db.start_record.side_effect = start
    agent_db.add_metadata.side_effect = annotate
    backup_bin = fake_claude(tmp_path / "backup", f"import sys\nsys.stdin.readline()\nprint({json.dumps(SUCCESS)!r})\n")
    backup = _settings("claude-code", backup_bin)
    backup = replace(backup, profile=replace(backup.profile, id="backup", name="Backup"))
    monkeypatch.setattr(worker, "fallback_settings", AsyncMock(return_value=backup))

    def run(runtime, launcher=Launcher.NATIVE, *, events=(), exit_code=1, wait=False, steer=False,
            error=ERROR, startup=True):
        transcript = "Launcher ready\n" + "\n".join(json.dumps(e) for e in [
            *(_launch_lines(runtime) if startup else []), *events])
        body = f'''
import sys, time
print({transcript!r}, flush=True)
print({error!r}, file=sys.stderr, flush=True)
if {wait!r}:
    time.sleep(30)
raise SystemExit({exit_code})
'''
        if steer:
            body = f'''
import json, sys
for line in sys.stdin:
    request = json.loads(line)
    method = request.get('method')
    if 'id' not in request:
        continue
    result = ({{'thread': {{'id': 'stand-in'}}}} if method == 'thread/start' else
              {{'turn': {{'id': 'turn'}}}} if method == 'turn/start' else {{}})
    print(json.dumps({{'id': request['id'], 'result': result}}), flush=True)
    if method == 'turn/start':
        for event in {list(events)!r}:
            print(json.dumps(event), flush=True)
        print({error!r}, file=sys.stderr, flush=True)
        raise SystemExit({exit_code})
'''
        executable = fake_claude(tmp_path / 'primary', body)
        primary = _settings(runtime, executable, launcher=launcher, fallback="backup")
        if steer:
            profile = primary.profile
            primary = replace(primary, profile=replace(profile, runtime=replace(
                profile.runtime, capabilities=profile.runtime.capabilities | {Capability.STEER})))
        monkeypatch.setattr(worker, "load_agent_settings", AsyncMock(return_value=primary))
        pool = worker.WorkerPool(config=worker.WorkerConfig(), runtime_root=tmp_path / "runtime")

        async def execute():
            return await asyncio.wait_for(pool._execute(_step_envelope(tmp_path / "work", verify=False)), 10)

        return asyncio.run(execute()), records

    yield run
    assert all(process.returncode is not None for process in processes)


@pytest.mark.parametrize(("runtime", "launcher"), LAUNCHES)
def test_nonzero_exit_before_model_event_uses_recorded_fallback(run_primary, agent_db, runtime, launcher):
    result, records = run_primary(runtime, launcher)
    assert result["status"] == "completed"
    expected = [{"from": "primary", "to": "backup", "reason": "exited before any model event: Unknown model"}]
    assert result["fallback"] == expected
    assert [r["metadata"]["profile"]["id"] for r in records] == ["primary", "backup"]
    assert records[1]["metadata"]["fallback"] == expected
    assert records[1]["metadata"]["fallback_from"] == "primary"
    assert "Unknown model" in agent_db.finish_record.await_args_list[0].kwargs["error"]


@pytest.mark.parametrize("runtime", ["cursor-agent", "codex", "coco"])
def test_ttadk_zero_exit_without_result_uses_bounded_recorded_fallback(
    run_primary, agent_db, monkeypatch, tmp_path, runtime,
):
    refusal = "Cannot use this model: unavailable-model. Available models: " + "available-model, " * 140
    result, records = run_primary(runtime, Launcher.TTADK, exit_code=0, startup=False, error=refusal)
    assert result["status"] == "completed"
    assert [r["metadata"]["profile"]["id"] for r in records] == ["primary", "backup"]
    change, = result["fallback"]
    reason = change["reason"]
    assert change["from"] == "primary" and change["to"] == "backup"
    assert reason.startswith("exited before any model event: Cannot use this model:")
    assert len(reason) < 300
    assert records[1]["metadata"]["fallback"] == [change]
    assert records[1]["metadata"]["fallback_from"] == "primary"
    assert refusal[:100] in agent_db.finish_record.await_args_list[0].kwargs["error"]
    activity = [c.kwargs["summary"] for c in agent_db.insert_activity.await_args_list
                if "Agent Profile fallback:" in c.kwargs["summary"]]
    assert len(activity) == 1 and activity[0].endswith(f"({reason})")

    agent = planner.PlannerAgent(task_id="task-1", runtime_root=tmp_path / "runtime")
    step = planner.PlanStep("s1", 0, "Inspect", status="dispatched", worker_id="worker-1")
    agent._plan = planner.TaskPlan("p1", "goal", "Inspect", str(tmp_path), steps=[step], status="executing")
    monkeypatch.setattr(agent, "_decide", AsyncMock())
    envelope = MessageEnvelope.create(
        source=worker_address("worker-1"), target=agent.agent_address, message_type=MessageType.WORK_RESULT,
        payload={**result, "task_id": "s1"})
    asyncio.run(asyncio.wait_for(agent._handle_work_result(envelope), 5))
    entries = [row for row in agent_db.store.journal["task-1"] if row["kind"] == "profile_fallback"]
    assert [json.loads(row["summary"])["reason"] for row in entries] == [reason]
    assert step.retries == 0


@pytest.mark.parametrize(("runtime", "launcher"), LAUNCHES)
@pytest.mark.parametrize("exit_code", [0, 1])
def test_one_model_event_then_exit_never_falls_back(run_primary, runtime, launcher, exit_code):
    event = ({"type": "item.started", "item": {"id": "call", "type": "command_execution", "command": "pwd"}}
             if runtime in ("codex", "coco") else ASSISTANT)
    result, records = run_primary(runtime, launcher, events=[event], exit_code=exit_code)
    assert result["status"] == "failed"
    assert "fallback" not in result
    assert len(records) == 1


@pytest.mark.parametrize("runtime", ["codex", "coco"])
@pytest.mark.parametrize("phase", ["started", "updated", "completed"])
def test_incomplete_model_item_prevents_fallback(run_primary, runtime, phase):
    result, records = run_primary(runtime, events=[{
        "type": f"item.{phase}", "item": {"id": "answer", "type": "agent_message", "text": "Inspecting"}}])
    assert result["status"] == "failed" and "fallback" not in result
    assert len(records) == 1


@pytest.mark.parametrize("runtime", ["claude-code", "grok", "codex", "coco", "cursor-agent"])
def test_reported_usage_without_answer_prevents_fallback(run_primary, runtime):
    events = [{"type": "result", "subtype": "error_during_execution", "is_error": True,
               "modelUsage": {"model": {"inputTokens": 1}}, "usage": {"inputTokens": 1}}]
    if runtime in ("codex", "coco"):
        events = [{"type": "turn.completed", "usage": {"input_tokens": 1}},
                  {"type": "turn.failed", "error": {"message": "Interrupted"}}]
    result, records = run_primary(runtime, events=events)
    assert result["status"] == "failed" and "fallback" not in result
    assert len(records) == 1


@pytest.mark.parametrize("runtime", [runtime for runtime, launcher in LAUNCHES if launcher is Launcher.NATIVE])
def test_native_zero_exit_without_model_event_does_not_fall_back(run_primary, agent_db, runtime):
    result, records = run_primary(runtime, exit_code=0)
    assert result["status"] == "failed" and "fallback" not in result
    assert len(records) == 1
    assert "Unknown model" in result["error"]
    assert agent_db.finish_record.await_args.kwargs["error"] == result["error"]


def test_timeout_without_model_event_does_not_fall_back(run_primary, monkeypatch):
    monkeypatch.setattr(worker, "_load_settings", AsyncMock(return_value=worker.WorkerSettings(0.2, 0, "ROLE")))
    with pytest.raises(asyncio.TimeoutError):
        run_primary("claude-code", wait=True)


def test_unverified_detection_keeps_existing_behavior(run_primary, monkeypatch):
    monkeypatch.setattr(type(ADAPTERS["claude-code"]), "first_event_known", False)
    result, records = run_primary("claude-code")
    assert result["status"] == "failed" and "fallback" not in result
    assert len(records) == 1


@pytest.mark.parametrize("runtime", ["claude-code", "grok"])
def test_zero_turn_model_refusal_can_fall_back_without_nonzero_exit(run_primary, runtime):
    result, records = run_primary(runtime, exit_code=0, events=[{
        "type": "assistant", "is_error": True, "message": {"model": "<synthetic>", "content": [
            {"type": "text", "text": "Model is unavailable"}]}}, {
        "type": "result", "subtype": "error_during_execution", "is_error": True, "num_turns": 0,
        "errors": ["Model is unavailable\nChoose another model"]}])
    assert result["status"] == "completed" and len(records) == 2
    assert result["fallback"][0]["reason"] == "exited before any model event: Model is unavailable"


@pytest.mark.parametrize(("runtime", "event"), [
    ("claude-code", {**INIT, "usage": {"input_tokens": 1}}),
    ("claude-code", {"type": "stream_event", "event": {
        "type": "message_start", "message": {"model": "reported-model", "content": []}}}),
    ("cursor-agent", {"type": "thinking", "subtype": "delta", "text": "Inspecting"}),
    ("cursor-agent", {"type": "tool_call", "subtype": "started", "call_id": "call",
                      "tool_call": {"shellToolCall": {"args": {"command": "pwd"}}}}),
])
def test_partial_response_or_tool_start_prevents_fallback(run_primary, runtime, event):
    result, records = run_primary(runtime, events=[event])
    assert result["status"] == "failed" and "fallback" not in result
    assert len(records) == 1


def test_cursor_retry_cannot_erase_previous_model_event(run_primary):
    result, records = run_primary("cursor-agent", events=[ASSISTANT, {"type": "retry", "subtype": "starting"}])
    assert result["status"] == "failed" and "fallback" not in result
    assert len(records) == 1


@pytest.mark.parametrize("runtime", ["claude-code", "grok"])
def test_model_event_prevents_fallback_even_if_result_reports_zero_turns(run_primary, runtime):
    result, records = run_primary(runtime, events=[ASSISTANT, {
        "type": "result", "subtype": "error_during_execution", "is_error": True, "num_turns": 0}])
    assert result["status"] == "failed" and "fallback" not in result
    assert len(records) == 1


def test_successful_result_survives_nonzero_teardown(run_primary):
    result, records = run_primary("claude-code", events=[SUCCESS])
    assert result["status"] == "completed" and "fallback" not in result
    assert len(records) == 1


@pytest.mark.parametrize("evidence", ["none", "zero_usage", "usage", "item"])
def test_steering_stream_preserves_model_evidence_before_exit(run_primary, evidence):
    events = []
    if evidence in ("usage", "zero_usage"):
        events = [{"method": "thread/tokenUsage/updated", "params": {
            "tokenUsage": {"total": {"inputTokens": 1 if evidence == "usage" else 0}}}}]
    elif evidence == "item":
        events = [{"method": "item/started", "params": {
            "item": {"id": "answer", "type": "agentMessage", "text": "Inspecting"}}}]
    result, records = run_primary("codex", steer=True, events=events)
    falls_back = evidence in ("none", "zero_usage")
    assert (result["status"] == "completed") is falls_back
    assert ("fallback" in result) is falls_back
    assert len(records) == (2 if falls_back else 1)


def test_whitespace_error_uses_exit_information(run_primary):
    result, records = run_primary("claude-code", error=" \n")
    assert result["status"] == "completed" and len(records) == 2
    assert result["fallback"][0]["reason"] == "exited before any model event: Claude Code exited with code 1"
