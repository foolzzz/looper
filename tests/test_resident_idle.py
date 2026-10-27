"""Resident idle admission uses runtime events, including work after a user result."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from fake_claude import fake_claude
from looper.agents.claude_cli import ResidentClaude, _Output
from looper.agents.run_usage import Usage
from looper.agents.runtimes import ResidentSession
from looper.agents.runtimes.grok_acp import AcpGrok
from looper.agents.runtimes.grok import ResidentGrok


def _resident() -> tuple[ResidentClaude, _Output]:
    resident = ResidentClaude(label="manager-idle-test")
    output = _Output(resident, asyncio.StreamReader(), None)
    resident._process = SimpleNamespace(pid=17, returncode=None)
    resident._output = output
    resident._command = ["claude"]
    resident._signature = "same-launch"
    return resident, output


async def _result(output: _Output) -> None:
    await output._dispatch({"type": "result", "total_cost_usd": 0.1})


def test_unknown_runtime_conservatively_refuses_idle_stop() -> None:
    assert asyncio.run(ResidentSession.try_stop_if_idle(object())) is False


def test_per_turn_session_idle_is_a_noop() -> None:
    resident = ResidentGrok(label="test")
    resident._command = ["grok"]
    assert asyncio.run(resident.try_stop_if_idle()) is False
    assert resident.command == ["grok"]


def test_grok_acp_active_request_refuses_idle_stop() -> None:
    resident = AcpGrok(label="test")
    resident._prompt_id = 1
    resident._pending[1] = object()
    assert asyncio.run(resident.try_stop_if_idle()) is False
    assert resident._prompt_id == 1


def test_grok_acp_without_background_proof_stays_resident() -> None:
    assert asyncio.run(AcpGrok(label="test").try_stop_if_idle()) is False


def test_unobserved_claude_cannot_prove_idle() -> None:
    async def run() -> None:
        resident, _ = _resident()
        resident.stop = AsyncMock()
        assert await resident.try_stop_if_idle() is False
        resident.stop.assert_not_awaited()
    asyncio.run(run())


def test_pending_user_message_refuses_idle_stop() -> None:
    async def run() -> None:
        resident, output = _resident()
        await _result(output)
        output.turn = object()
        resident.stop = AsyncMock()
        assert await resident.try_stop_if_idle() is False
        resident.stop.assert_not_awaited()
    asyncio.run(run())


def test_autonomous_turn_refuses_idle_stop_until_handler_finishes() -> None:
    async def run() -> None:
        resident, output = _resident()
        await _result(output)
        await output._dispatch({"type": "system", "subtype": "init"})
        resident.stop = AsyncMock()
        assert await resident.try_stop_if_idle() is False
        resident.stop.assert_not_awaited()
        await _result(output)
        assert output.idle
    asyncio.run(run())


def test_autonomous_result_handler_stays_busy_while_saving() -> None:
    async def run() -> None:
        resident, output = _resident()
        await _result(output)
        entered, finish = asyncio.Event(), asyncio.Event()
        class Handler:
            async def event(self, event: dict) -> None:
                pass
            async def end(self, turn: object) -> None:
                entered.set()
                await finish.wait()
        async def autonomous(tag: object) -> Handler:
            return Handler()
        output._autonomous = autonomous
        await output._dispatch({"type": "system", "subtype": "init"})
        result = asyncio.create_task(_result(output))
        await entered.wait()
        assert await resident.try_stop_if_idle() is False
        finish.set()
        await result
        assert output.idle
    asyncio.run(run())


@pytest.mark.parametrize("fixture", ["background_task_race", "background_agent", "agent_team_worker"])
def test_recorded_background_jobs_protect_gap_after_first_result(fixture: str) -> None:
    async def run() -> None:
        resident, output = _resident()
        resident.stop = AsyncMock()
        events = [json.loads(line) for line in
                  (Path(__file__).parent / "fixtures" / "claude_stream" / f"{fixture}.jsonl").read_text().splitlines()]
        first_result = next(index for index, event in enumerate(events) if event.get("type") == "result")
        for event in events[:first_result + 1]:
            if "type" in event:
                await output._dispatch(event)
        assert not output._own_open
        assert output._pending_jobs
        assert await resident.try_stop_if_idle() is False
        resident.stop.assert_not_awaited()
        for event in events[first_result + 1:]:
            if "type" in event:
                await output._dispatch(event)
        assert output.idle
    asyncio.run(run())


def test_background_launch_without_task_identifier_stays_protected() -> None:
    async def run() -> None:
        resident, output = _resident()
        await output._dispatch({"type": "system", "subtype": "init"})
        await output._dispatch({"type": "assistant", "message": {"content": [
            {"type": "tool_use", "id": "tool-1", "name": "Bash", "input": {"run_in_background": True}},
        ]}})
        await _result(output)
        resident.stop = AsyncMock()
        assert await resident.try_stop_if_idle() is False
        resident.stop.assert_not_awaited()
    asyncio.run(run())


def test_malformed_job_lifecycle_keeps_resident() -> None:
    async def run() -> None:
        resident, output = _resident()
        await output._dispatch({"type": "system", "subtype": "task_started"})
        await _result(output)
        assert not output.idle
        assert await resident.try_stop_if_idle() is False
    asyncio.run(run())


def test_notification_waiting_for_autonomous_turn_is_not_idle() -> None:
    async def run() -> None:
        _, output = _resident()
        await _result(output)
        await output._dispatch({"type": "system", "subtype": "task_notification", "task_id": "job-1",
                                "status": "completed"})
        assert not output._pending_jobs
        assert not output.idle
    asyncio.run(run())


def test_concurrent_idle_stop_claims_resident_once() -> None:
    async def run() -> None:
        resident, output = _resident()
        await _result(output)
        entered, finish = asyncio.Event(), asyncio.Event()
        async def close() -> None:
            entered.set()
            await finish.wait()
        resident.stop = AsyncMock(side_effect=close)
        stopping = asyncio.create_task(resident.try_stop_if_idle())
        await entered.wait()
        assert await resident.try_stop_if_idle() is False
        finish.set()
        assert await stopping is True
        resident.stop.assert_awaited_once()
    asyncio.run(run())


def test_failed_idle_close_restores_launch_state_and_can_retry() -> None:
    async def run() -> None:
        resident, output = _resident()
        await _result(output)
        process = resident._process
        totals = resident.totals
        async def fail() -> None:
            resident._process, resident._output = None, None
            resident._command, resident._signature = [], None
            raise OSError("close failed")
        resident.stop = AsyncMock(side_effect=fail)
        with pytest.raises(OSError, match="close failed"):
            await resident.try_stop_if_idle()
        assert resident._process is process and resident._output is output
        assert resident.command == ["claude"] and resident._signature == "same-launch"
        assert resident.totals == totals
        resident.stop = AsyncMock()
        assert await resident.try_stop_if_idle() is True
    asyncio.run(run())


def test_idle_cleanup_failure_log_excludes_exception_content(caplog: pytest.LogCaptureFixture) -> None:
    async def run() -> None:
        resident, output = _resident()
        await _result(output)
        resident._stop_process = AsyncMock(side_effect=OSError("private runtime output"))
        with pytest.raises(OSError):
            await resident.try_stop_if_idle()
        assert resident.started
        assert "manager-idle-test: idle resident cleanup failed (OSError)" in caplog.text
        assert "private runtime output" not in caplog.text
    asyncio.run(run())


def test_failed_close_with_closed_stdin_is_stale_for_resume() -> None:
    async def run() -> None:
        resident, _ = _resident()
        resident._process.stdin = SimpleNamespace(is_closing=lambda: True)
        assert resident.stale("same-launch")
    asyncio.run(run())


def test_idle_stop_closes_real_fake_process_and_preserves_totals(tmp_path: Path) -> None:
    fake = fake_claude(tmp_path / "claude", """import json, sys
for raw in sys.stdin:
    message = json.loads(raw)
    for state in ('queued', 'started'):
        print(json.dumps({'type': 'command_lifecycle', 'state': state, 'command_uuid': message['uuid']}), flush=True)
    print(json.dumps({'type': 'system', 'subtype': 'init', 'session_id': 'saved-session'}), flush=True)
    print(json.dumps({'type': 'result', 'session_id': 'saved-session', 'total_cost_usd': 0.4}), flush=True)
""")
    async def run() -> None:
        resident = ResidentClaude(label="manager-idle-test")
        await resident.start([fake], cwd=str(tmp_path), signature=1, totals=Usage(cost_usd=0.2))
        async def event(event: dict) -> None:
            pass
        try:
            turn = await resident.run_turn("hello", event)
            assert turn.result_event["session_id"] == "saved-session"
            assert turn.usage.cost_usd == pytest.approx(0.2)
            assert await resident.try_stop_if_idle() is True
            assert resident.pid is None and not resident.started
            assert resident.totals.cost_usd == pytest.approx(0.4)
        finally:
            await resident.stop(kill=True)
    asyncio.run(run())
