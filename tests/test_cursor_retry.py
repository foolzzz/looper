"""Retry Cursor's exhausted reconnects through real local stand-in processes."""
from __future__ import annotations

import asyncio
import json
import os
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from fake_claude import fake_claude
from looper.agents.runtimes import Launcher, cursor_login, ttadk
from looper.agents.tiers import worker
from test_agent_planner_worker import _step_envelope, _worker_row
from test_cursor_agent import CURSOR, _launch, _prompt
from test_ttadk_cursor_agent import _cursor_script

RETRY_LINE = "Cursor's CLI gave up after reconnecting; retried once"
SIGNATURE = "RetriableError: WritableIterable is closed\nError: command failed unexpectedly."


def _stand_in(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str, *, wrapped: bool = False,
              verify: bool = False):
    calls = tmp_path / "calls.jsonl"
    report = {"passed": True, "findings": [], "summary": "recovered"} if verify else {
        "status": "completed", "changed_files": {}, "summary": "recovered"}
    body = f'''
import json, os, sys, time
from pathlib import Path
if sys.argv[1:] == ['status', '--format', 'json']:
    print('{{"status":"authenticated","isAuthenticated":true}}'); sys.exit(0)
log = Path({str(calls)!r})
text = sys.stdin.read()
with log.open('a') as out:
    out.write(json.dumps({{'pid': os.getpid(), 'argv': sys.argv[1:], 'stdin': text,
                          'config': os.environ['CURSOR_CONFIG_DIR']}}) + '\\n')
attempt = len(log.read_text().splitlines())
mode = {mode!r}
if mode == 'lost-init' and attempt <= 2: mode = 'once'
if mode not in ('no-chat', 'lost-init'):
    print(json.dumps({{'type': 'system', 'subtype': 'init', 'session_id': 'chat-retry'}}), flush=True)
if mode == 'result':
    print(json.dumps({{'type': 'result', 'subtype': 'error', 'is_error': True}}))
if attempt == 1 or mode in ('twice', 'other', 'result', 'timeout', 'signal', 'lost-init', 'exit2'):
    print(json.dumps({{'type': 'connection', 'subtype': 'reconnecting'}}), flush=True)
    print({SIGNATURE!r} if mode != 'other' else 'unrelated failure', file=sys.stderr, flush=True)
    if mode == 'lost-init': print('x' * 1200, file=sys.stderr, flush=True)
    print('attempt-' + str(attempt), file=sys.stderr, flush=True)
    if mode == 'timeout': time.sleep(10)
    if mode == 'signal': os.kill(os.getpid(), 15)
    sys.exit(2 if mode == 'exit2' else 1)
print(json.dumps({{'type': 'assistant', 'message': {{'content': [{{'type': 'text', 'text': {json.dumps(report)!r}}}]}}}}))
print(json.dumps({{'type': 'result', 'subtype': 'success'}}))
'''
    executable = _cursor_script(tmp_path / "cursor-agent", body) if wrapped else fake_claude(tmp_path / "cursor-agent", body)
    launch = _launch(executable=executable)
    if wrapped:
        wrapper = fake_claude(tmp_path / "ttadk", '''
import subprocess, sys
assert sys.argv[1:6] == ['code', '-t', 'cursor-cli', '--skip-check', '--']
subprocess.run(['agent', *sys.argv[6:]], check=False)
sys.exit(0)
''')
        launch = replace(launch, settings=replace(launch.settings, profile=replace(launch.profile,
            runtime=replace(launch.profile.runtime, executable=wrapper, launcher=Launcher.TTADK))))
    monkeypatch.setenv("PATH", str(tmp_path) + os.pathsep + os.environ["PATH"])
    monkeypatch.setenv("HOME", str(tmp_path / "operator"))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "operator"))
    return launch, calls


@pytest.mark.parametrize(("mode", "wrapped", "verify", "attempts"), [
    ("once", False, False, 2), ("once", False, True, 2),
    ("twice", False, False, 2), ("other", False, False, 1),
    ("result", False, False, 1), ("signal", False, False, 1), ("exit2", False, False, 1),
    ("once", True, False, 2), ("twice", True, False, 2), ("other", True, False, 1),
])
def test_worker_cursor_retry(mode, wrapped, verify, attempts, tmp_path, monkeypatch, agent_db: Any):
    launch, calls = _stand_in(tmp_path, monkeypatch, mode, wrapped=wrapped, verify=verify)
    _worker_row(monkeypatch, {"system_prompt": "ROLE", "timeout_seconds": 5, "max_turns": 0})

    async def settings(*args, **kwargs):
        return launch.settings

    monkeypatch.setattr(worker, "load_agent_settings", settings)
    work = tmp_path / "work"
    work.mkdir()

    async def run():
        pool = worker.WorkerPool(config=worker.WorkerConfig(), runtime_root=tmp_path / "runtime")
        return await pool._execute(_step_envelope(work, verify=verify), is_verify=verify)

    result = asyncio.run(asyncio.wait_for(run(), 15))
    invocations = [json.loads(line) for line in calls.read_text().splitlines()]
    assert len(invocations) == attempts
    if attempts == 2:
        assert invocations[0]["pid"] != invocations[1]["pid"]
        assert invocations[0]["stdin"] == invocations[1]["stdin"]
        assert invocations[0]["config"] == invocations[1]["config"]
        assert invocations[0]["argv"] == invocations[1]["argv"]
    events = [event for call in agent_db.append_events.await_args_list for _, event in call.args[1]]
    retries = [event for event in events if event.get("subtype") == "cursor_run_retry"]
    assert len(retries) == attempts - 1
    if retries:
        assert retries[0]["reason"] == "transient_backend" and "RetriableError" in retries[0]["stderr"]
    activities = [call.kwargs["summary"] for call in agent_db.insert_activity.await_args_list]
    assert activities.count(RETRY_LINE) == attempts - 1
    if mode == "once":
        assert result.get("passed") is True if verify else result["status"] == "completed"
        assert agent_db.finish_record.await_args.kwargs["error"] is None
    else:
        assert result["status"] == "failed"
        if mode == "twice":
            assert "attempt-1" in result["error"] and "attempt-2" in result["error"]
            assert "attempt-1" in result["stderr"] and "attempt-2" in result["stderr"]


@pytest.mark.parametrize(("mode", "wrapped", "attempts"), [
    ("once", False, 2), ("no-chat", False, 2), ("twice", False, 2),
    ("other", False, 1), ("result", False, 1), ("signal", False, 1),
    ("once", True, 2), ("twice", True, 2), ("other", True, 1),
])
def test_session_cursor_retry(mode, wrapped, attempts, tmp_path, monkeypatch):
    launch, calls = _stand_in(tmp_path, monkeypatch, mode, wrapped=wrapped)
    command = CURSOR.resident_command(launch.profile.runtime.executable, cwd=tmp_path,
                                     prompt_file=_prompt(tmp_path / "runtime"), launch=launch)
    events = []

    async def run():
        session = CURSOR.resident_session("manager")
        async def on_event(event):
            events.append(event)
        await session.start(command, cwd=str(tmp_path), signature="same", env=launch.environment())
        try:
            result = await session.run_turn("same turn", on_event)
            assert session.stale("same") is (mode not in ("once", "no-chat", "result"))
            return result
        finally:
            await session.stop()

    result = asyncio.run(asyncio.wait_for(run(), 15))
    invocations = [json.loads(line) for line in calls.read_text().splitlines()]
    assert len(invocations) == attempts
    assert len([event for event in events if event.get("subtype") == "cursor_run_retry"]) == attempts - 1
    if attempts == 2:
        assert invocations[0]["pid"] != invocations[1]["pid"]
        assert invocations[0]["config"] == invocations[1]["config"]
        if mode == "no-chat":
            assert "--resume" not in invocations[1]["argv"]
            assert invocations[0]["stdin"] == invocations[1]["stdin"]
        else:
            assert invocations[1]["argv"][-2:] == ["--resume", "chat-retry"]
            assert invocations[1]["stdin"] == "same turn"
    if mode in ("once", "no-chat"):
        assert result.result_event["subtype"] == "success"
    elif mode == "twice":
        assert result.result_event is None
        assert "attempt-1" in result.stderr and "attempt-2" in result.stderr


@pytest.fixture(autouse=True)
def _forget_login():
    cursor_login.forget_probe()
    ttadk.forget_probe()
    yield
    cursor_login.forget_probe()
    ttadk.forget_probe()


@pytest.mark.parametrize("ending", ["cancel", "timeout", "cancel-before-retry"])
def test_session_stop_and_timeout_do_not_retry(ending, tmp_path, monkeypatch):
    from looper.agents.runtimes.cancel import TurnCancel

    launch, calls = _stand_in(tmp_path, monkeypatch, "once" if ending == "cancel-before-retry" else "timeout")
    command = CURSOR.resident_command(launch.profile.runtime.executable, cwd=tmp_path,
                                     prompt_file=_prompt(tmp_path / "runtime"), launch=launch)

    async def run():
        session = CURSOR.resident_session("planner-test")
        cancel = TurnCancel()
        connected = asyncio.Event()
        async def on_event(event):
            if event.get("subtype") == "cursor_connection":
                connected.set()
                if ending == "cancel":
                    cancel.request()
            if event.get("subtype") == "cursor_run_retry" and ending == "cancel-before-retry":
                cancel.request()
        await session.start(command, cwd=str(tmp_path), signature="same", env=launch.environment())
        task = asyncio.create_task(session.run_turn("same turn", on_event, cancel=cancel))
        try:
            await asyncio.wait_for(connected.wait(), 5)
            if ending == "timeout":
                with pytest.raises(asyncio.TimeoutError):
                    await asyncio.wait_for(task, 0.05)
            else:
                result = await task
                assert result.stopped is not None
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            await session.stop()

    asyncio.run(asyncio.wait_for(run(), 10))
    assert len(calls.read_text().splitlines()) == 1


def test_worker_timeout_does_not_retry(tmp_path, monkeypatch, agent_db: Any):
    launch, calls = _stand_in(tmp_path, monkeypatch, "timeout")
    _worker_row(monkeypatch, {"system_prompt": "ROLE", "timeout_seconds": 1, "max_turns": 0})
    async def settings(*args, **kwargs):
        return launch.settings
    monkeypatch.setattr(worker, "load_agent_settings", settings)
    work = tmp_path / "work"
    work.mkdir()
    async def run():
        pool = worker.WorkerPool(config=worker.WorkerConfig(), runtime_root=tmp_path / "runtime")
        with pytest.raises(asyncio.TimeoutError):
            await pool._execute(_step_envelope(work, verify=False))
    asyncio.run(asyncio.wait_for(run(), 10))
    assert len(calls.read_text().splitlines()) == 1
    events = [event for call in agent_db.append_events.await_args_list for _, event in call.args[1]]
    assert not any(event.get("subtype") == "cursor_run_retry" for event in events)


@pytest.mark.parametrize("role", ["manager", "planner"])
@pytest.mark.parametrize("mode", ["once", "twice", "lost-init"])
def test_tier_turn_records_cursor_retry(role, mode, tmp_path, monkeypatch, agent_db: Any):
    from unittest.mock import AsyncMock

    from looper.agents.bus import MessageBus
    from looper.agents.message import MANAGER_ADDRESS, MessageEnvelope, MessageType
    from looper.agents.system_prompt import SystemPrompt
    from looper.agents.tiers.manager import ManagerAgent, ManagerConfig
    from looper.agents.tiers.planner import PlannerAgent, PlannerTurnError, TaskPlan
    from tests.test_manager_reply import WEB

    launch, calls = _stand_in(tmp_path, monkeypatch, mode)

    async def run():
        bus = MessageBus(runtime_root=tmp_path / "runtime")
        if role == "manager":
            agent = ManagerAgent(config=ManagerConfig(work_dir=tmp_path), runtime_root=tmp_path / "runtime")
            agent._bus = bus
            agent._composed_prompt = lambda _: SystemPrompt("PROMPT")
        else:
            agent = PlannerAgent(task_id="task-1", runtime_root=tmp_path / "runtime")
            agent._plan = TaskPlan("plan-1", "goal-1", "Inspect", str(tmp_path))
            monkeypatch.setattr(agent, "_system_prompt", lambda *args, **kwargs: SystemPrompt("PROMPT"))
        agent._load_agent_settings = AsyncMock(return_value=launch.settings)
        try:
            if mode == "lost-init":
                if role == "manager":
                    await agent._handle(MessageEnvelope.create(source=WEB, target=MANAGER_ADDRESS,
                        message_type=MessageType.USER_MESSAGE, payload={"text": "first"}))
                else:
                    assert "recovered" in await agent._turn("first")
            if role == "manager":
                await agent._handle(MessageEnvelope.create(source=WEB, target=MANAGER_ADDRESS,
                    message_type=MessageType.USER_MESSAGE, payload={"text": "hello"}))
            elif mode in ("twice", "lost-init"):
                with pytest.raises(PlannerTurnError, match="attempt-"):
                    await agent._turn("hello")
            else:
                assert "recovered" in await agent._turn("hello")
        finally:
            await agent.stop()
            await bus.shutdown()

    asyncio.run(asyncio.wait_for(run(), 15))
    invocations = [json.loads(line) for line in calls.read_text().splitlines()]
    assert len(invocations) == (4 if mode == "lost-init" else 2)
    assert invocations[-1]["argv"][-2:] == ["--resume", "chat-retry"]
    events = [event for call in agent_db.append_events.await_args_list for _, event in call.args[1]]
    retry_count = 2 if mode == "lost-init" else 1
    assert len([event for event in events if event.get("subtype") == "cursor_run_retry"]) == retry_count
    finish = agent_db.finish_record.await_args.kwargs
    if mode != "once":
        first, second = (3, 4) if mode == "lost-init" else (1, 2)
        assert f"attempt-{first}" in finish["error"] and f"attempt-{second}" in finish["error"]
    else:
        assert finish["error"] is None
    if role == "planner":
        assert sum(call.kwargs["summary"] == RETRY_LINE for call in agent_db.insert_activity.await_args_list) == retry_count


def test_missing_cursor_login_does_not_launch(tmp_path, monkeypatch, agent_db: Any):
    launch, calls = _stand_in(tmp_path, monkeypatch, "once")
    _worker_row(monkeypatch, {"system_prompt": "ROLE", "timeout_seconds": 5, "max_turns": 0})
    async def settings(*args, **kwargs):
        return replace(launch.settings, profile_error="Cursor is not signed in on this machine")
    monkeypatch.setattr(worker, "load_agent_settings", settings)
    work = tmp_path / "work"
    work.mkdir()
    async def run():
        pool = worker.WorkerPool(config=worker.WorkerConfig(), runtime_root=tmp_path / "runtime")
        return await pool._execute(_step_envelope(work, verify=False))
    result = asyncio.run(asyncio.wait_for(run(), 5))
    assert result["status"] == "failed" and "not signed in" in result["error"]
    assert not calls.exists()
