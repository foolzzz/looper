"""Profile fallback through real process streams, with disposable stand-in CLIs."""

import asyncio
import json
import sys
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from fake_claude import fake_claude
from looper.agents import log_redaction
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
SECRET = "synthetic-registered-launch-secret"
SUCCESS = {"type": "result", "subtype": "success", "result": json.dumps({
    "status": "completed", "summary": "Recovered", "changed_files": {}})}


def _settings(runtime, executable, *, launcher=Launcher.NATIVE, fallback=None):
    adapter = ADAPTERS[runtime]
    vendor = {"claude-code": VendorType.CLAUDE_SUBSCRIPTION, "codex": VendorType.CODEX_LOGIN,
              "coco": VendorType.COCO_LOGIN, "cursor-agent": VendorType.CURSOR_LOGIN,
              "grok": VendorType.GROK_LOGIN, "acp": VendorType.ACP_COMMAND}[runtime]
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
            error=ERROR, startup=True, fallback=True, verify=False, web_search=True, attempt_id=None):
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
        primary = _settings(runtime, executable, launcher=launcher, fallback="backup" if fallback else None)
        primary = replace(primary, profile=replace(primary.profile, web_search=web_search))
        if steer:
            profile = primary.profile
            primary = replace(primary, profile=replace(profile, runtime=replace(
                profile.runtime, capabilities=profile.runtime.capabilities | {Capability.STEER})))
        monkeypatch.setattr(worker, "load_agent_settings", AsyncMock(return_value=primary))
        pool = worker.WorkerPool(config=worker.WorkerConfig(), runtime_root=tmp_path / "runtime")

        async def execute():
            envelope = _step_envelope(tmp_path / "work", verify=verify)
            envelope.payload["attempt_id"] = attempt_id
            return await asyncio.wait_for(pool._execute(
                envelope, is_verify=verify), 10)

        return asyncio.run(execute()), records

    yield run
    assert all(process.returncode is not None for process in processes)


def test_profile_fallback_runs_share_the_attempt(run_primary, monkeypatch, agent_db):
    from looper.agents import attempts

    started, finished = AsyncMock(), AsyncMock()
    monkeypatch.setattr(attempts, "worker_started", started)
    monkeypatch.setattr(attempts, "worker_finished", finished)
    result, records = run_primary("claude-code", attempt_id=41)
    assert result["status"] == "completed"
    assert [call.args for call in started.await_args_list] == [(41, 1, "primary"), (41, 2, "backup")]
    assert [call.args for call in finished.await_args_list] == [
        (41, worker.RunOutcome.RUN_FAILED, attempts.RunFailure.CRASH),
        (41, worker.RunOutcome.COMPLETED, None),
    ]
    assert [call.kwargs["outcome"] for call in agent_db.finish_record.await_args_list] == [
        "run_failed", "completed"]
    assert all("attempt_id" not in record["input_text"] for record in records)


def test_fallback_releases_then_admits_the_new_account(run_primary, monkeypatch):
    from contextlib import asynccontextmanager
    from looper.agents import admission

    backup = worker.fallback_settings.return_value
    resolved = backup.profile.resolved_model
    backup = replace(backup, profile=replace(backup.profile, resolved_model=replace(
        resolved, provider=replace(resolved.provider, id="backup-account"),
        model=replace(resolved.model, provider_id="backup-account"),
        endpoint=replace(resolved.endpoint, provider_id="backup-account"))))
    monkeypatch.setattr(worker, "fallback_settings", AsyncMock(return_value=backup))
    events = []

    @asynccontextmanager
    async def slot(provider_id, lane, *, owner, **kwargs):
        events.append(("enter", provider_id))
        try:
            yield
        finally:
            events.append(("exit", provider_id))

    monkeypatch.setattr(admission, "slot", slot)
    result, _ = run_primary("claude-code")
    assert result["status"] == "completed"
    assert events == [("enter", "provider"), ("exit", "provider"),
                      ("enter", "backup-account"), ("exit", "backup-account")]


@pytest.mark.parametrize(("events", "exit_code", "failure"), [
    ([], 2, "crash"),
    ([{"type": "result", "subtype": "error_max_budget_usd", "is_error": True}], 1, "run_cap"),
    ([{"type": "result", "subtype": "error_during_execution", "is_error": True}], 1, None),
    ([SUCCESS], 1, None),
])
def test_attempt_failure_uses_runtime_signals(run_primary, monkeypatch, events, exit_code, failure):
    from looper.agents import attempts

    finished = AsyncMock()
    monkeypatch.setattr(attempts, "worker_started", AsyncMock())
    monkeypatch.setattr(attempts, "worker_finished", finished)
    result, _ = run_primary("claude-code", attempt_id=41, events=events, exit_code=exit_code, fallback=False)
    assert finished.await_count == 1
    assert finished.await_args.args[2] == (attempts.RunFailure(failure) if failure else None)
    assert finished.await_args.args[1] == (worker.RunOutcome.COMPLETED if events == [SUCCESS]
                                          else worker.RunOutcome.RUN_FAILED)


def test_attempt_timeout_marker_survives_timeout_propagation(run_primary, monkeypatch):
    from looper.agents import attempts

    finished = AsyncMock()
    monkeypatch.setattr(attempts, "worker_started", AsyncMock())
    monkeypatch.setattr(attempts, "worker_finished", finished)
    monkeypatch.setattr(worker, "_load_settings", AsyncMock(return_value=worker.WorkerSettings(0.2, 0, "ROLE")))
    with pytest.raises(asyncio.TimeoutError):
        run_primary("claude-code", wait=True, attempt_id=41)
    finished.assert_awaited_once_with(41, worker.RunOutcome.RUN_FAILED, attempts.RunFailure.TIMEOUT)


def test_reviewer_links_record_without_counting_worker_run(run_primary, monkeypatch):
    from looper.agents import attempts

    link, started, finished = AsyncMock(), AsyncMock(), AsyncMock()
    monkeypatch.setattr(attempts, "link_record", link)
    monkeypatch.setattr(attempts, "worker_started", started)
    monkeypatch.setattr(attempts, "worker_finished", finished)
    event = {**SUCCESS, "result": json.dumps({"passed": True, "summary": "checked", "findings": []})}
    result, _ = run_primary("claude-code", attempt_id=41, events=[event], verify=True)
    assert result["passed"]
    link.assert_awaited_once_with(41, 1)
    started.assert_not_awaited()
    finished.assert_not_awaited()


@pytest.mark.parametrize("writer", ["worker_started", "worker_finished", "link_record"])
def test_attempt_database_failure_keeps_the_run_usable(run_primary, monkeypatch, caplog, writer):
    from looper.agents import attempts
    from looper.db import step_attempts

    writes = {name: AsyncMock(side_effect=ConnectionError("synthetic database failure") if name == writer else None)
              for name in ("worker_started", "worker_finished", "link_record")}
    for name, operation in writes.items():
        operation.__name__ = name
        monkeypatch.setattr(step_attempts, name, operation)
    review = writer == "link_record"
    event = {**SUCCESS, "result": json.dumps({"passed": True, "summary": "checked", "findings": []})} \
        if review else SUCCESS
    result, _ = run_primary("claude-code", attempt_id=41, events=[event], verify=review)
    assert result["passed"] if review else result["status"] == "completed"
    writes[writer].assert_awaited_once()
    assert any(record.name == attempts.__name__ and writer in record.message for record in caplog.records)


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


@pytest.mark.parametrize(("runtime", "launcher", "exit_code"), [
    *[(runtime, Launcher.TTADK, 0) for runtime in ("cursor-agent", "codex", "coco")],
    *[(runtime, launcher, 1) for runtime, launcher in LAUNCHES],
])
def test_launch_refusal_uses_bounded_redacted_recorded_fallback(
    run_primary, agent_db, monkeypatch, tmp_path, runtime, launcher, exit_code,
):
    monkeypatch.setattr(log_redaction, "_secrets", (SECRET,))
    # The first secret crosses the reason's clipping boundary; the second crosses stderr's.
    refusal = "Cannot use this model: " + "x" * 235 + SECRET + "y" * 200 + SECRET + ". Available models: "
    refusal += "available-model, " * 140
    result, records = run_primary(runtime, launcher, exit_code=exit_code, startup=False, error=refusal)
    assert result["status"] == "completed"
    assert [r["metadata"]["profile"]["id"] for r in records] == ["primary", "backup"]
    change, = result["fallback"]
    reason = change["reason"]
    assert change["from"] == "primary" and change["to"] == "backup"
    assert reason.startswith("exited before any model event: Cannot use this model:")
    assert len(reason) < 300
    assert records[1]["metadata"]["fallback"] == [change]
    assert records[1]["metadata"]["fallback_from"] == "primary"
    error = agent_db.finish_record.await_args_list[0].kwargs["error"]
    assert refusal[:100] in error
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
    for text in [error, reason, json.dumps(records), *activity, *(row["summary"] for row in entries)]:
        assert log_redaction.MASK in text and SECRET not in text
        assert SECRET[:8] not in text
    assert step.retries == 0


@pytest.mark.parametrize("launcher", list(Launcher))
@pytest.mark.parametrize("exit_code", [0, 1])
@pytest.mark.parametrize("secret", [SECRET, "sk-ABC123" + "synthetic" * 20])
def test_stderr_error_is_redacted_before_clipping(run_primary, agent_db, monkeypatch, launcher, exit_code, secret):
    monkeypatch.setattr(log_redaction, "_secrets", (SECRET,))
    refusal = "Unknown model: " + "x" * 476 + " " + secret + " trailing diagnostic"
    result, records = run_primary("codex", launcher, exit_code=exit_code, error=refusal, fallback=False)
    assert result["status"] == "failed" and len(records) == 1
    assert agent_db.finish_record.await_args.kwargs["error"] == result["error"]
    for text in [result["error"], result["stderr"]]:
        assert "Unknown model" in text
        if secret == SECRET:
            assert log_redaction.MASK in text
        assert secret[:8] not in text


@pytest.mark.parametrize("launcher", list(Launcher))
@pytest.mark.parametrize("fallback", [False, True])
def test_cli_stderr_masks_unregistered_keys_and_strips_ansi(run_primary, agent_db, launcher, fallback):
    keys = ("sk-ABC123syntheticKey", "syntheticApiKey", "syntheticBearerToken")
    stderr = (f"\x1b]8;;https://example.invalid/model\x1b\\\x1b[31mUnknown model: {keys[0]} "
              f"api_key={keys[1]} Bearer {keys[2]}\x1b[0m\x1b]8;;\x1b\\")
    result, records = run_primary("codex", launcher, error=stderr, fallback=fallback)
    error = agent_db.finish_record.await_args_list[0].kwargs["error"]
    assert "Unknown model" in error
    assert len(records) == (2 if fallback else 1)
    texts = [error]
    if fallback:
        reason = result["fallback"][0]["reason"]
        assert "Unknown model" in reason
        assert records[1]["metadata"]["fallback"] == result["fallback"]
        activity = [c.kwargs["summary"] for c in agent_db.insert_activity.await_args_list
                    if "Agent Profile fallback:" in c.kwargs["summary"]]
        assert len(activity) == 1 and activity[0].endswith(f"({reason})")
        texts.extend([reason, json.dumps(records), *activity])
    else:
        texts.extend([result["error"], result["stderr"]])
    for text in texts:
        assert "[redacted]" in text
        assert all(key not in text for key in keys)
        assert "\x1b" not in text and "\\u001b" not in text
        assert "example.invalid" not in text


@pytest.mark.parametrize("launcher", list(Launcher))
@pytest.mark.parametrize("evidence", ["none", "item", "usage", "no-fallback-profile"])
@pytest.mark.parametrize("runtime", ["codex", "coco"])
def test_codex_zero_exit_error_result_preserves_refusal_and_model_evidence(
    run_primary, agent_db, monkeypatch, launcher, evidence, runtime,
):
    monkeypatch.setattr(log_redaction, "_secrets", (SECRET,))
    # Replay the recorded Codex JSONL shape; the adapter emits errors without num_turns.
    failed = json.loads((Path(__file__).parent / "fixtures/codex_exec/failed.jsonl").read_text().splitlines()[-1])
    refusal = f"Model is unavailable: {SECRET}. " + "Choose another model. " * 100
    failed["error"]["message"] = refusal
    events = ([{"type": "item.started", "item": {"id": "answer", "type": "agent_message", "text": "Inspecting"}}]
              if evidence == "item" else [{"type": "turn.completed", "usage": {"input_tokens": 1}}]
              if evidence == "usage" else [])
    result, records = run_primary(runtime, launcher, events=[*events, failed], exit_code=0, error="",
                                  fallback=evidence != "no-fallback-profile")
    falls_back = evidence == "none"
    assert (result["status"] == "completed") is falls_back
    assert ("fallback" in result) is falls_back
    assert len(records) == (2 if falls_back else 1)
    error = agent_db.finish_record.await_args_list[0].kwargs["error"]
    texts = [error, result["fallback"][0]["reason"]] if falls_back else [error, result["error"]]
    for text in texts:
        assert "Model is unavailable" in text and log_redaction.MASK in text and SECRET not in text
        assert len(text) <= 500


def test_codex_reviewer_records_refusal_reason(run_primary, agent_db):
    refusal = "Model is unavailable: \x1b[31msk-ABC123syntheticKey\x1b[0m; choose another model"
    result, records = run_primary("codex", events=[{
        "type": "turn.failed", "error": {"message": refusal}}], exit_code=0, error="",
        fallback=False, verify=True)
    assert result["passed"] is False and len(records) == 1
    error = agent_db.finish_record.await_args.kwargs["error"]
    assert error == result["summary"]
    assert "Model is unavailable" in error and "choose another model" in error
    assert "sk-ABC123" not in error and "\x1b" not in error


def test_codex_reviewer_negative_verdict_is_not_a_run_error(run_primary, agent_db):
    verdict = {"passed": False, "findings": ["Missing validation"], "summary": "Changes needed"}
    result, _ = run_primary("codex", events=[{
        "type": "item.completed", "item": {"id": "review", "type": "agent_message", "text": json.dumps(verdict)}},
        {"type": "turn.completed", "usage": {"input_tokens": 1}}], exit_code=0, error="", verify=True)
    assert result["passed"] is False and result["summary"] == verdict["summary"]
    assert agent_db.finish_record.await_args.kwargs["error"] is None


@pytest.mark.parametrize(("runtime", "launcher"), LAUNCHES)
@pytest.mark.parametrize("exit_code", [0, 1])
def test_one_model_event_then_exit_never_falls_back(run_primary, runtime, launcher, exit_code):
    event = ({"type": "item.started", "item": {"id": "call", "type": "command_execution", "command": "pwd"}}
             if runtime in ("codex", "coco") else ASSISTANT)
    result, records = run_primary(runtime, launcher, events=[event], exit_code=exit_code)
    assert result["status"] == "failed"
    assert "fallback" not in result
    assert len(records) == 1


@pytest.mark.parametrize("runtime", ["codex", "cursor-agent"])
@pytest.mark.parametrize("launcher", list(Launcher))
@pytest.mark.parametrize("exit_code", [0, 1])
@pytest.mark.parametrize("model_event", [False, True])
def test_exit_before_prompt_delivery_preserves_output_and_fallback_policy(
    run_primary, agent_db, monkeypatch, runtime, launcher, exit_code, model_event,
):
    spawn = worker.proc_tree.spawn
    first = True

    async def exited_process(*args, **kwargs):
        nonlocal first
        process = await spawn(*args, **kwargs)
        # Only the primary exits before start() writes; a fallback must still receive its prompt.
        if first:
            first = False
            await asyncio.wait_for(process.wait(), 5)
        return process

    monkeypatch.setattr(worker.proc_tree, "spawn", exited_process)
    event = ({"type": "item.started", "item": {"id": "call", "type": "command_execution", "command": "pwd"}}
             if runtime == "codex" else ASSISTANT)
    result, records = run_primary(runtime, launcher, events=[event] if model_event else [], exit_code=exit_code)
    falls_back = not model_event and (exit_code != 0 or launcher is Launcher.TTADK)
    assert result["status"] == ("completed" if falls_back else "failed")
    assert ("fallback" in result) is falls_back
    assert len(records) == (2 if falls_back else 1)
    error = agent_db.finish_record.await_args_list[0].kwargs["error"]
    assert "Unknown model" in error
    assert "user turn" not in error
    if falls_back:
        assert "Unknown model" in result["fallback"][0]["reason"]
    else:
        assert result["error"] == error
        assert "Unknown model" in result["stderr"]


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


@pytest.mark.parametrize("web_search", [False, True])
def test_grok_fallback_uses_each_profiles_web_search_setting(run_primary, monkeypatch, tmp_path, web_search):
    executable = fake_claude(tmp_path / "grok-backup", f"import sys\nsys.stdin.read()\nprint({json.dumps(SUCCESS)!r})\n")
    backup = _settings("grok", executable)
    backup = replace(backup, profile=replace(backup.profile, id="backup", name="Backup", web_search=not web_search))
    monkeypatch.setattr(worker, "fallback_settings", AsyncMock(return_value=backup))
    result, records = run_primary("grok", web_search=web_search)
    assert result["status"] == "completed"
    assert [r["metadata"]["profile"]["id"] for r in records] == ["primary", "backup"]
    assert ["--disable-web-search" in r["metadata"]["command"] for r in records] == [not web_search, web_search]


@pytest.mark.parametrize("acp_end", ["source", "target"])
def test_acp_fallback_hop_is_refused_when_loading_stored_profiles(agent_db, monkeypatch, acp_end):
    async def run():
        from looper.agents import agent_settings
        from looper.agents.agent_settings import AgentType
        from looper.agents.runtimes import RuntimeUnsupported
        from looper.db import profile_registry

        primary = _settings("acp" if acp_end == "source" else "claude-code", "stand-in", fallback="backup")
        backup = _settings("claude-code" if acp_end == "source" else "acp", "stand-in")
        monkeypatch.setattr(profile_registry, "get_profile", AsyncMock(return_value={"id": "backup"}))
        monkeypatch.setattr(agent_settings, "_stored_profile", lambda row: backup.profile)
        with pytest.raises(RuntimeUnsupported, match="ACP|acp"):
            await agent_settings.fallback_settings(primary, AgentType.REVIEWER)
    asyncio.run(run())


@pytest.mark.parametrize("case", ["success_stays_alive", "disabled", "unreadable_settings", "timeout",
                                  "run_budget", "task_budget", "vault_unavailable"])
def test_acp_worker_records_refusals_and_ends_the_process_after_a_verdict(agent_db, monkeypatch, tmp_path, case):
    async def run():
        from looper.agents import acp_reviewer_settings
        from looper.agents.runtimes import acp_policy

        _worker_row(monkeypatch, {"system_prompt": "ROLE", "timeout_seconds": 0, "max_turns": 0})
        if case == "timeout":
            monkeypatch.setattr(worker, "_load_settings", AsyncMock(return_value=worker.WorkerSettings(0.2, 0, "ROLE")))
        settings = _settings("acp", "", fallback="backup")
        if case == "run_budget":
            settings = replace(settings, max_budget_usd=0.10)
        if case == "task_budget":
            agent_db.get_task_project.return_value = {"task_budget_usd": 1.0}
        monkeypatch.setattr(worker, "load_agent_settings", AsyncMock(return_value=settings))
        config = acp_reviewer_settings.AcpReviewerSettings(
            enabled=case != "disabled", command=(sys.executable, str(Path(__file__).with_name("fake_acp.py")),
                                                "--scenario", "hang" if case == "timeout" else "success_stays_alive"),
            model="unavailable-model", account="Stand-in login")
        read = AsyncMock(return_value=config, side_effect=ValueError("acp_reviewer.command unreadable")
                         if case == "unreadable_settings" else None)
        monkeypatch.setattr(acp_reviewer_settings, "read", read)
        policy = SimpleNamespace(wrap=lambda: list(config.command), environment=lambda env: env,
                                 record={"reason": "ok"}, cleanup=Mock())
        prepare = AsyncMock(return_value=policy)
        monkeypatch.setattr(acp_policy, "prepare_policy", prepare)
        vault_root = tmp_path / "knowledge"
        monkeypatch.setattr(worker.vault, "load", AsyncMock(return_value=(None, "unreadable vault")
            if case == "vault_unavailable" else (SimpleNamespace(path=vault_root), None)))
        spawn = worker.proc_tree.spawn
        processes = []
        async def tracked_spawn(*args, **kwargs):
            process = await spawn(*args, **kwargs)
            processes.append(process)
            return process
        monkeypatch.setattr(worker.proc_tree, "spawn", tracked_spawn)
        fallback = AsyncMock()
        monkeypatch.setattr(worker, "fallback_settings", fallback)
        pool = worker.WorkerPool(config=worker.WorkerConfig(), runtime_root=tmp_path / "runtime")
        execution = pool._execute(_step_envelope(tmp_path, verify=True), is_verify=True)
        if case == "timeout":
            with pytest.raises(asyncio.TimeoutError, match=r"ACP reviewer .*0\.2 s"):
                await asyncio.wait_for(execution, 8)
        else:
            result = await asyncio.wait_for(execution, 8)
            assert result["passed"] is (case == "success_stays_alive")
        fallback.assert_not_awaited()
        assert agent_db.finish_record.await_args.kwargs["outcome"] == (
            "passed" if case == "success_stays_alive" else "run_failed")
        if case in {"success_stays_alive", "timeout"}:
            assert len(processes) == 1 and processes[0].returncode is not None
            assert agent_db.finish_record.await_args.kwargs["cost_usd"] is None
            policy.cleanup.assert_called_once()
            assert prepare.await_args.kwargs["runtime_root"] == tmp_path / "runtime"
            assert prepare.await_args.kwargs["vault"] == vault_root
        else:
            assert not processes
            assert "acp_reviewer" in result["summary"]
            if case in {"run_budget", "task_budget"}:
                assert "budget" in result["summary"]
                prepare.assert_not_awaited()
                assert agent_db.add_metadata.await_args.args[1]["acp_write_policy"]["reason"] == "budget_cap_unsupported"
    asyncio.run(run())
