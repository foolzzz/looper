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
from looper.agents.message import MANAGER_ADDRESS, MessageEnvelope, MessageType, channel_address
from looper.agents.runtimes import Launcher
from looper.agents.system_prompt import SystemPrompt
from looper.agents.tiers import manager, planner, worker
from test_agent_planner_worker import _step_envelope, _worker_row
from test_ttadk_runtime import CODEX, _launch

SIGN_IN_ERROR = "Sign-in expired; run ttadk auth login sso"
SECRET = "synthetic-sign-in-secret"


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
