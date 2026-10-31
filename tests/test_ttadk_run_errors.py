"""TTADK's successful wrapper exit must preserve a safe, useful failure reason."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest

from fake_claude import fake_claude
from looper.agents import log_redaction
from looper.agents.bus import MessageBus
from looper.agents.message import MANAGER_ADDRESS, MessageEnvelope, MessageType, channel_address, worker_address
from looper.agents.runtimes import Launcher
from looper.agents.system_prompt import SystemPrompt
from looper.agents.tiers import manager, planner, worker
from test_agent_planner_worker import _step_envelope, _worker_row
from test_ttadk_runtime import CODEX, _launch

SIGN_IN_ERROR = "Sign-in expired; run ttadk auth login sso"
SECRET = "synthetic-sign-in-secret"


@pytest.mark.parametrize("text, limit, expected", [
    (None, 8, ""),
    ("  short\n error\t", 20, "short error"),
    ("  short\n error\t", 11, "short error"),
    ("  prefix\n middle\t reason  ", 10, "pre…reason"),
])
def test_error_clipping_keeps_both_ends(text: Any, limit: int, expected: str) -> None:
    assert planner._clip_ends(text, limit) == expected


def test_failed_review_summary_keeps_its_final_reason(tmp_path: Path) -> None:
    agent = planner.PlannerAgent(task_id="t1", runtime_root=tmp_path)
    step = planner.PlanStep(step_id="s1", index=0, instruction="Inspect", reviewer_id="r1")
    summary = "Review failed: " + "diagnostic\n" * 150 + SIGN_IN_ERROR
    report = agent._review_report(step, {"passed": False, "summary": summary})
    assert report[-1].startswith("- summary: Review failed: ")
    assert report[-1].endswith(SIGN_IN_ERROR)
    assert len(report[-1]) == len("- summary: ") + 1000
    outcome = planner.result_outcome({"verify_passed": False, "verify_summary": summary})
    assert outcome.startswith("review failed: Review failed: ") and outcome.endswith(SIGN_IN_ERROR)
    # Findings still take precedence, retaining both ends within the same limit.
    finding = "finding " * 40
    assert planner.result_outcome({"verify_passed": False, "verify_findings": [finding],
                                   "verify_summary": summary}) == (
                                       f"review failed: {finding.strip()[:66]}…{finding.strip()[-133:]}")


@pytest.fixture
def failed_ttadk(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "operator"))
    monkeypatch.setattr(log_redaction, "_secrets", (SECRET,))
    return fake_claude(tmp_path / "ttadk", (
        "import sys\n"
        "sys.stdin.read()\n"
        "print('discarded-prefix-' + 'x' * 2000, file=sys.stderr)\n"
        f"print('TTADK_OPENAI_API_KEY=synthetic-api-token {SECRET}', file=sys.stderr)\n"
        f"print({SIGN_IN_ERROR!r}, file=sys.stderr)\n"
        "sys.exit(0)\n"
    ))


def _assert_diagnostic(error: str, agent_db: Any) -> None:
    assert SIGN_IN_ERROR in error
    assert SECRET not in error and "synthetic-api-token" not in error
    assert "discarded-prefix" not in error
    assert len(error) <= 1200
    recorded = agent_db.finish_record.call_args.kwargs["error"]
    assert recorded == error
    activities = [call.kwargs["summary"] for call in agent_db.insert_activity.call_args_list
                  if call.kwargs.get("kind") == "error"]
    assert any(SIGN_IN_ERROR in line for line in activities)
    assert all(SECRET not in line and "synthetic-api-token" not in line for line in activities)


@pytest.mark.parametrize("verify", [False, True], ids=["worker", "reviewer"])
def test_ttadk_step_without_result_records_sign_in_stderr(
    verify: bool, failed_ttadk: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, agent_db: Any,
) -> None:
    _worker_row(monkeypatch, {"system_prompt": "ROLE", "timeout_seconds": 5, "max_turns": 0})
    monkeypatch.setattr(worker, "load_agent_settings", AsyncMock(return_value=_launch(executable=failed_ttadk).settings))

    async def run() -> dict[str, Any]:
        pool = worker.WorkerPool(config=worker.WorkerConfig(), runtime_root=tmp_path / "runtime")
        return await pool._execute(_step_envelope(tmp_path / "work", verify=verify), is_verify=verify)

    result = asyncio.run(asyncio.wait_for(run(), 8))
    assert result["status"] == "failed"
    _assert_diagnostic(result["error"], agent_db)
    assert SECRET not in result["stderr"] and "synthetic-api-token" not in result["stderr"]
    assert len(result["stderr"]) == 1000 and result["error"].endswith(result["stderr"])
    assert any(call.kwargs.get("summary") == result["error"]
               for call in agent_db.insert_activity.await_args_list)

    agent = planner.PlannerAgent(task_id="t1", runtime_root=tmp_path / "runtime")
    step = planner.PlanStep(step_id="s1", index=0, instruction="Inspect", worker_id="w1", reviewer_id="r1",
                            status="completed" if verify else "dispatched")
    agent._plan = planner.TaskPlan("p1", "g1", "Inspect", str(tmp_path), steps=[step], status="executing")
    turn = AsyncMock(return_value='```json\n{"decision": "fail", "reason": "run failed"}\n```')
    monkeypatch.setattr(agent, "_turn", turn)
    monkeypatch.setattr(agent, "_apply_decision", AsyncMock())
    envelope = MessageEnvelope.create(
        source=worker_address("r1" if verify else "w1"), target=agent.agent_address,
        message_type=MessageType.VERIFY_RESULT if verify else MessageType.WORK_RESULT,
        payload={**result, "task_id": "s1"},
    )
    handler = agent._handle_verify_result if verify else agent._handle_work_result
    asyncio.run(asyncio.wait_for(handler(envelope), 5))
    report = agent._review_report(step, result) if verify else agent._work_report(step, result)
    outcome = planner.result_outcome(step.result)
    activity = agent_db.insert_activity.await_args.kwargs
    for text in [report[-1], outcome, activity["summary"], turn.await_args.args[0]]:
        assert SIGN_IN_ERROR in text
        assert SECRET not in text and "synthetic-api-token" not in text
    assert activity["kind"] == "error"
    if verify:
        assert step.result["verify_passed"] is False
        assert step.result["verify_summary"] == result["error"]
        assert report[-1].startswith("- review run failed: ")
    else:
        assert result["error"].split(":", 1)[0] in report[-1]
        assert result["error"].split(":", 1)[0] in outcome


@pytest.mark.parametrize("role", [False, True], ids=["builtin", "role"])
@pytest.mark.parametrize("error", [
    "Reviewer timed out before producing a verdict",
    "Reviewer timed out: " + "diagnostic " * 30 + SIGN_IN_ERROR + "." * (100 - len(SIGN_IN_ERROR)),
], ids=["short", "long-tail-boundary"])
def test_planner_keeps_reviewer_run_error_without_a_verdict(
    role: bool, error: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, agent_db: Any,
) -> None:
    agent = planner.PlannerAgent(task_id="t1", runtime_root=tmp_path)
    step = planner.PlanStep(step_id="s1", index=0, instruction="Inspect", status="completed", reviewer_id="r1",
                            result={"status": "completed"})
    if role:
        step.review_roles = [planner.StepReviewRole("security")]
        step.review_stage, step.review_position = planner.ReviewStage.ROLE, 0
    agent._plan = planner.TaskPlan("p1", "g1", "Inspect", str(tmp_path), steps=[step], status="executing")
    decide = AsyncMock()
    monkeypatch.setattr(agent, "_decide", decide)
    envelope = MessageEnvelope.create(
        source=worker_address("r1"), target=agent.agent_address, message_type=MessageType.VERIFY_RESULT,
        payload={"task_id": "s1", "status": "failed", "error": error},
    )
    asyncio.run(asyncio.wait_for(agent._handle_verify_result(envelope), 5))
    assert error[-100:] in planner.result_outcome(step.result)
    assert error[-100:] in agent_db.insert_activity.await_args.kwargs["summary"]
    assert error in "\n".join(decide.await_args.args[3])
    assert decide.await_args.args[2].succeeded is False


def test_native_omp_reviewer_failure_keeps_its_final_reason_in_closing_note(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, agent_db: Any,
) -> None:
    from test_omp_runtime import _launch as omp_launch

    error = "Review failed: " + "diagnostic " * 22 + SIGN_IN_ERROR + "." * (100 - len(SIGN_IN_ERROR))
    executable = fake_claude(tmp_path / "omp", (
        "import json, sys\n"
        "sys.stdin.read()\n"
        "print(json.dumps({'type': 'message_end', 'message': {'role': 'assistant', "
        f"'stopReason': 'error', 'content': [], 'errorMessage': {error!r}}}}}), flush=True)\n"
        "print(json.dumps({'type': 'agent_end', 'isTerminal': True}), flush=True)\n"
        "sys.exit(1)\n"
    ))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "operator"))
    _worker_row(monkeypatch, {"system_prompt": "ROLE", "timeout_seconds": 5, "max_turns": 0})
    launch = omp_launch(executable=executable)
    assert launch.runtime.type == "omp" and launch.profile.runtime.launcher is Launcher.NATIVE
    monkeypatch.setattr(worker, "load_agent_settings", AsyncMock(return_value=launch.settings))
    agent = planner.PlannerAgent(task_id="t1", runtime_root=tmp_path / "runtime")
    step = planner.PlanStep(step_id="s1", index=0, instruction="Inspect", status="completed", reviewer_id="r1",
                            result={"status": "completed"})
    agent._plan = planner.TaskPlan("p1", "g1", "Inspect", str(tmp_path), steps=[step], status="executing")
    monkeypatch.setattr(agent, "_decide", AsyncMock())

    async def run() -> dict[str, Any]:
        pool = worker.WorkerPool(config=worker.WorkerConfig(), runtime_root=tmp_path / "runtime")
        result = await pool._execute(_step_envelope(tmp_path / "work", verify=True), is_verify=True)
        await agent._handle_verify_result(MessageEnvelope.create(
            source=worker_address("r1"), target=agent.agent_address, message_type=MessageType.VERIFY_RESULT,
            payload={**result, "task_id": "s1"},
        ))
        return result

    result = asyncio.run(asyncio.wait_for(run(), 8))
    assert result["passed"] is False
    assert result["findings"] == [result["summary"]]
    assert result["summary"].startswith("OMP run did not succeed: Review failed: ")
    assert result["summary"].endswith(error[-100:])
    outcome = planner.result_outcome(step.result)
    assert outcome.startswith("review failed: OMP run did not succeed: ")
    assert outcome.endswith(error[-100:])
    assert len(outcome) == len("review failed: ") + 200
    note = planner.closing_note("Review could not finish", [(step.label, step.status, step.result)])
    assert outcome in note and note.endswith(error[-100:])


def test_planner_failed_worker_summary_keeps_its_final_reason(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, agent_db: Any,
) -> None:
    agent = planner.PlannerAgent(task_id="t1", runtime_root=tmp_path)
    step = planner.PlanStep(step_id="s1", index=0, instruction="Inspect", worker_id="w1", status="dispatched")
    agent._plan = planner.TaskPlan("p1", "g1", "Inspect", str(tmp_path), steps=[step], status="executing")
    decide = AsyncMock()
    monkeypatch.setattr(agent, "_decide", decide)
    summary = "Worker failed: " + "diagnostic " * 150 + SIGN_IN_ERROR + "." * (100 - len(SIGN_IN_ERROR))
    result = worker.judge_result(
        {"subtype": "success", "result": '{"status": "failed", "summary": "' + summary + '"}'},
        work_dir=tmp_path, is_verify=False,
    )
    assert result["error"] == result["summary"] == summary
    envelope = MessageEnvelope.create(
        source=worker_address("w1"), target=agent.agent_address, message_type=MessageType.WORK_RESULT,
        payload={**result, "task_id": "s1"},
    )
    asyncio.run(asyncio.wait_for(agent._handle_work_result(envelope), 5))
    report = decide.await_args.args[3]
    summary_line = next(line for line in report if line.startswith("- summary: "))
    assert summary_line.startswith("- summary: Worker failed: ")
    assert summary_line.endswith(summary[-100:])
    assert len(summary_line) == len("- summary: ") + 1000
    assert not any(line.startswith("- error: ") for line in report)
    assert decide.await_args.args[2].succeeded is False


@pytest.mark.parametrize("verify", [False, True], ids=["work", "review"])
def test_planner_success_reports_keep_the_existing_clipping(
    verify: bool, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, agent_db: Any,
) -> None:
    agent = planner.PlannerAgent(task_id="t1", runtime_root=tmp_path)
    step = planner.PlanStep(step_id="s1", index=0, instruction="Inspect", worker_id="w1", reviewer_id="r1",
                            status="completed" if verify else "dispatched")
    agent._plan = planner.TaskPlan("p1", "g1", "Inspect", str(tmp_path), steps=[step], status="executing")
    monkeypatch.setattr(agent, "_decide", AsyncMock())
    summary = "All checks passed.\n" * 80
    flat = " ".join(summary.split())
    result = {"task_id": "s1", "summary": summary, **({"passed": True} if verify else {"status": "completed"})}
    envelope = MessageEnvelope.create(
        source=worker_address("r1" if verify else "w1"), target=agent.agent_address,
        message_type=MessageType.VERIFY_RESULT if verify else MessageType.WORK_RESULT, payload=result,
    )
    handler = agent._handle_verify_result if verify else agent._handle_work_result
    asyncio.run(asyncio.wait_for(handler(envelope), 5))
    report = agent._review_report(step, result) if verify else agent._work_report(step, result)
    assert report == (["Review of step s1 by r1:", "- passed: yes"] if verify else
                      ["Result of step s1 from w1:", "- status: completed"]) + [f"- summary: {flat[:999]}…"]
    assert agent_db.insert_activity.await_args.kwargs["summary"] == (
        f"Verify passed: {summary[:150]}" if verify else summary[:200])
    if not verify:
        assert planner.result_outcome(step.result) == f"completed: {flat[:399]}…"


@pytest.mark.parametrize("resuming", [False, True], ids=["new-session", "resume"])
def test_ttadk_planner_without_result_records_sign_in_stderr(
    resuming: bool, failed_ttadk: str, tmp_path: Path, agent_db: Any,
) -> None:
    launch = _launch(executable=failed_ttadk)
    prompt = SystemPrompt("Inspect the project.")
    command = CODEX.resident_command(failed_ttadk, cwd=tmp_path,
                                    prompt_file=prompt.write_file(tmp_path / "runtime"), launch=launch)
    session_id = "expired-thread" if resuming else None
    if session_id:
        command += CODEX.resume_args(session_id)

    async def run() -> str:
        agent = planner.PlannerAgent(task_id="failed-sign-in", runtime_root=tmp_path / "runtime")
        agent._plan = planner.TaskPlan("plan", "goal", "Inspect", str(tmp_path))
        agent._claude = CODEX.resident_session("planner-failed-sign-in")
        try:
            await agent._claude.start(command, cwd=str(tmp_path), signature="same", env=launch.environment())
            with pytest.raises(planner._SessionNotResumed if resuming else planner.PlannerTurnError) as failure:
                await agent._exchange("Inspect", planner._Process(command, None, prompt, launch), 5,
                                      resuming=session_id)
            return str(failure.value)
        finally:
            await agent._claude.stop()

    error = asyncio.run(asyncio.wait_for(run(), 8))
    _assert_diagnostic(error, agent_db)


@pytest.mark.parametrize("launcher", [Launcher.TTADK, Launcher.NATIVE])
@pytest.mark.parametrize("resuming", [False, True], ids=["new-session", "resume"])
def test_manager_without_result_reports_stderr_only_for_unreliable_exit_codes(
    launcher: Launcher, resuming: bool, failed_ttadk: str, tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch, agent_db: Any,
) -> None:
    launch = _launch(launcher, executable=failed_ttadk)
    monkeypatch.setattr(manager, "load_agent_settings", AsyncMock(return_value=launch.settings))
    if resuming:
        agent_db.store.sessions["manager"] = {
            "session_id": "expired-thread", "session_totals": {}, "runtime_type": "codex",
            "profile_id": launch.profile.id, "provider_id": launch.profile.provider.id,
        }

    async def run() -> dict[str, Any]:
        root = tmp_path / "runtime"
        bus = MessageBus(runtime_root=root)
        agent = manager.ManagerAgent(config=manager.ManagerConfig(work_dir=tmp_path), runtime_root=root)
        agent._bus = bus
        source = channel_address("web")
        queue = bus.get_queue(source.agent_id)
        try:
            await agent._handle(MessageEnvelope.create(
                source=source, target=MANAGER_ADDRESS, message_type=MessageType.USER_MESSAGE,
                payload={"text": "hello"}, correlation_id="failed-sign-in",
            ))
            replies = []
            while not queue.empty():
                envelope = queue.get_nowait()
                if envelope.message_type == MessageType.AGENT_REPLY:
                    assert envelope.correlation_id == "failed-sign-in"
                    replies.append(envelope.payload)
            assert len(replies) == 1
            return replies[0]
        finally:
            await agent.stop()
            await bus.shutdown()

    reply = asyncio.run(asyncio.wait_for(run(), 8))
    runs = [call.kwargs for call in agent_db.finish_record.await_args_list]
    assert len(runs) == (2 if resuming else 1)
    assert all(run["status"] == "failed" for run in runs)
    commands = [call.kwargs["metadata"]["command"] for call in agent_db.start_record.await_args_list]
    assert ["resume" in command for command in commands] == ([True, False] if resuming else [False])
    if resuming:
        assert "expired-thread" in runs[0]["error"]
        assert "retried in a new session" in runs[0]["error"]
    for error in [reply["text"], reply["error"], *(run["error"] for run in runs)]:
        assert (SIGN_IN_ERROR in error) is (launcher is Launcher.TTADK)
        assert SECRET not in error and "synthetic-api-token" not in error
        assert "discarded-prefix" not in error
        assert len(error) <= 1200
